# -*- coding: utf-8 -*-
"""游戏素材流水线 5.0：分类独立生成 + 异常透明 + 质量守门 + 可控精修。

八个子阶段（store.STAGES）各自独立入队、独立进度、独立失败：
    plan(策划) → master(基准图) → scene / character / pet / weapon / building / monster

所有阶段进同一条 FIFO 串行工作队列（保显存顺序），类与类之间互不拖累。
"all" 是编排入口：按依赖顺序串起来，已完成（done）的跳过 = 续做语义。

5.0 新增：
  - 质量守门（quality.py）：黑图/纯色/低熵/跑色 四重检测，不过关自动换 seed 重试
  - 透明底（transmat.py）：按类指定 chroma 色键 / rembg，出图后即转 RGBA
  - 按类画幅（presets.ASPECTS）：场景 16:9、角色 2:3、武器/宠物/野怪 1:1、建筑 4:3
  - 筛选重跑：enqueue 带 opts（failed / tone / season），只重跑命中的条目
  - 阶段取消：cancel_stage()，逐张检查取消标志，已出的素材保留
  - 多候选：gen_variants() 一次出 N 张候选，选优提交（提交时旧图进版本历史）
  - 多参考图：_run_job 接受 refs 列表（基准图 + 角色当前图），换装/多姿态一致性
"""

from __future__ import annotations

import asyncio
import base64
import threading
import traceback
from collections import deque
from pathlib import Path

from shared import repo
from shared.config import settings
from shared.models_util import new_id
from engine.sdk import engine

from . import quality, store, tonecheck, transmat
from .planner import plan_from_idea
from .presets import (ASPECTS, get_preset, asset_size, transparent_mode,
                      SEASONS, WEAPON_TIERS, ASSET_KINDS, build_stage_items,
                      GENDERS, prompt_extra, prompt_negative)
from .styles import get_style

_MASTER_SIZE = (1152, 640)
_PLANNING_TIMEOUT = 300        # LLM 策划硬上限（秒），超时取消并降级规则
_SEASON_THRESHOLD_FACTOR = 1.6
_MAX_ATTEMPTS = 3              # 质量守门重试上限（含首次）
_MAX_CANDIDATES = 4            # 单张多候选上限
_MAX_HISTORY = 5               # 单张版本历史上限
_ASSET_KINDS = ASSET_KINDS     # noqa: 兼容旧名引用
_PLAN_KEYS = {"scene": "scenes", "character": "characters", "pet": "pets",
              "weapon": "weapons", "building": "buildings", "monster": "monsters"}
_COMMON_NEG = ("text, watermark, signature, ui, frame, border, "
               "low quality, blurry, deformed")
_ISOLATED_BG = ("isolated on plain pure white background, no environment, "
                "no ground shadow, product-shot style")


# ============================================================
#  队列：单 worker 串行消费，异常全部落库
# ============================================================

_queue: deque[tuple[str, str, dict]] = deque()
_worker_task: asyncio.Task | None = None
_current: dict[str, str] = {}          # sid -> 正在跑的 kind
_cancel_flags: set[str] = set()        # 被请求取消的 sid（当前运行中的）


def is_busy(sid: str) -> bool:
    """该集是否有阶段在排队/执行（前端 alive 标志 + 删除守门用）。"""
    if _current.get(sid):
        return True
    if any(sid == s and k != "all" for s, k, _ in _queue):
        return True
    s = store.get_set(sid)
    if not s:
        return False
    return any(st["status"] in ("queued", "running")
               for st in s["pipeline"]["stages"].values())


def stage_status(sid: str, kind: str) -> str:
    s = store.get_set(sid)
    return s["pipeline"]["stages"][kind]["status"] if s else "idle"


def enqueue(sid: str, kind: str, opts: dict | None = None) -> None:
    """把一个阶段（或 all）加入队列。依赖不满足时抛 ValueError。

    opts（仅素材类阶段）：{"filter": "failed"|"tone"|"season", "season": id}
    """
    s = store.get_set(sid)
    if not s:
        raise ValueError("项目不存在")
    if kind not in store.STAGE_IDS and kind != "all":
        raise ValueError(f"未知阶段：{kind}")
    if kind in store.STAGE_IDS and stage_status(sid, kind) in ("queued", "running"):
        raise ValueError(f"「{store.STAGE_LABELS.get(kind, '全部')}」已在队列中")
    if kind == "master" and not s.get("plan"):
        raise ValueError("还没有策划方案，请先点「自动策划」或「全部生成」")
    if kind in ASSET_KINDS:
        if not s.get("plan"):
            raise ValueError("还没有策划方案，请先点「自动策划」或「全部生成」")
        if not s.get("master"):
            raise ValueError("基准图还没生成，请先点「基准图」")
        opts = opts or {}
        if opts.get("filter") == "season" and opts.get("season") not in SEASONS:
            raise ValueError(f"未知季节：{opts.get('season')}")
        if opts.get("filter") == "gender" and opts.get("gender") not in GENDERS:
            raise ValueError(f"未知性别：{opts.get('gender')}")

    if kind in store.STAGE_IDS:
        def _mark(t: dict) -> None:
            t["pipeline"]["stages"][kind]["status"] = "queued"
            t["pipeline"]["stages"][kind]["error"] = ""
        store.mutate(sid, _mark)
    label = store.STAGE_LABELS.get(kind, "全部生成")
    suffix = ""
    if opts and opts.get("filter"):
        f = opts["filter"]
        if f == "failed":
            suffix = "（仅失败条目）"
        elif f == "tone":
            suffix = "（仅跑色条目）"
        elif f == "season" and opts.get("season") in SEASONS:
            suffix = f"（仅 {SEASONS[opts['season']]['name']}季）"
        elif f == "gender" and opts.get("gender") in GENDERS:
            suffix = f"（仅 {GENDERS[opts['gender']]['name']}版）"
        if kind in ASSET_KINDS:
            store.append_log(sid, "info", f"「{label}」筛选重跑{suffix}")
    _queue.append((sid, kind, opts or {}))
    _ensure_worker()


def cancel_stage(sid: str) -> dict:
    """请求取消：运行中的阶段在下一张图前停下；还在排队的直接摘除。"""
    result: dict = {"canceled_queue": [], "marked_running": False}
    if sid in _current:
        _cancel_flags.add(sid)
        result["marked_running"] = True
        store.append_log(sid, "warn", "已请求取消：当前图完成后停止（已出素材保留）")
    kept: deque = deque()
    while _queue:
        item = _queue.popleft()
        if item[0] == sid and item[1] != "all":
            result["canceled_queue"].append(item[1])

            def _unmark(t: dict, k=item[1]) -> None:
                st = t["pipeline"]["stages"][k]
                if st["status"] == "queued":
                    st["status"] = "idle"
            store.mutate(sid, _unmark)
        else:
            kept.append(item)
    _queue.extend(kept)
    if result["canceled_queue"]:
        store.append_log(sid, "info",
                         "已从队列移除：" + "、".join(
                             store.STAGE_LABELS[k] for k in result["canceled_queue"]))
    return result


def _canceled(sid: str) -> bool:
    return sid in _cancel_flags


def _ensure_worker() -> None:
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker())


async def _worker() -> None:
    try:
        while _queue:
            sid, kind, opts = _queue.popleft()
            _current[sid] = kind
            try:
                if kind == "all":
                    await _run_all(sid)
                else:
                    await _run_stage(sid, kind, opts)
            except asyncio.CancelledError:
                # 服务关闭/重启：把状态落库，别让页面永远"进行中"
                _mark_stage(sid, kind, status="failed", error="服务停止，任务中断（可重试）")
                store.append_log(sid, "error", "服务停止，任务中断")
                raise
            except _StageCanceled:
                _mark_stage(sid, kind, status="canceled", error="用户取消（已出素材保留）")
                store.append_log(sid, "warn", f"「{store.STAGE_LABELS.get(kind, '')}」已取消")
            except Exception as exc:
                print(f"[gameassets] 阶段 {kind} 异常：{exc}")
                traceback.print_exc()
                _mark_stage(sid, kind, status="failed", error=str(exc)[:200])
                store.append_log(sid, "error",
                                 f"「{store.STAGE_LABELS.get(kind, '全部生成')}」异常：{exc}")
            finally:
                _current.pop(sid, None)
                _cancel_flags.discard(sid)
    finally:
        global _worker_task
        _worker_task = None


class _StageCanceled(RuntimeError):
    """用户取消（区别于失败：已出素材保留，不算错误）。"""


async def _run_all(sid: str) -> None:
    """编排：plan → master → 六类。已完成（done）的阶段跳过——「全部生成」
    即「续做到全齐」，中断后点生成不会重跑已出的素材；要重做某一类用该类的单独重试。"""
    skipped: list[str] = []
    for kind in ("plan", "master", *ASSET_KINDS):
        if stage_status(sid, kind) == "done":
            skipped.append(store.STAGE_LABELS[kind])
            continue
        try:
            await _run_stage(sid, kind, {})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 单类失败不断链——记日志，继续下一类
            print(f"[gameassets] [all] {kind} 失败，继续：{exc}")
            store.append_log(sid, "error", f"「{store.STAGE_LABELS[kind]}」失败：{exc}")
    if skipped:
        store.append_log(sid, "info", f"续做：已完成类跳过（{'、'.join(skipped)}）")


def _mark_stage(sid: str, kind: str, *, status: str, error: str = "") -> None:
    if kind not in store.STAGE_IDS:      # "all" 是编排入口，没有自己的状态
        return

    def _apply(t: dict) -> None:
        t["pipeline"]["stages"][kind]["status"] = status
        t["pipeline"]["stages"][kind]["error"] = error[:200]
    store.mutate(sid, _apply)


def _reset_items(sid: str, kind: str, only_ids: set[str] | None = None) -> list[dict]:
    """把条目重置为 pending。only_ids 给定时只重置命中的（筛选重跑），返回执行清单。"""
    items: list[dict] = []

    def _apply(t: dict) -> None:
        st = t["pipeline"]["stages"][kind]
        for it in st["items"]:
            if only_ids is not None and it.get("id") not in only_ids:
                continue
            it["status"] = "pending"
            it["error"] = ""
            it["score"] = None
        st["done"] = sum(1 for it in st["items"] if it["status"] in ("done", "failed"))
        st["status"] = "running"
        st["error"] = ""
        items.extend(it for it in st["items"]
                     if only_ids is None or it.get("id") in only_ids)
    store.mutate(sid, _apply)
    return items


def _save_plan_and_items(sid: str, plan: dict) -> None:
    """策划完成：落方案 + 预展开六类条目（前端立刻能看到每类要出几张）。"""
    def _apply(t: dict) -> None:
        old = {k: t["pipeline"]["stages"][k]["items"] for k in ASSET_KINDS}
        t["plan"] = plan
        if plan.get("planner") == "llm":
            t["name"] = plan["title"]
        blueprint = build_stage_items(plan, old)
        for kind in ASSET_KINDS:
            st = t["pipeline"]["stages"][kind]
            st["items"] = blueprint[kind]
            st["total"] = len(blueprint[kind])
        master_st = t["pipeline"]["stages"]["master"]
        master_st["items"] = [{"id": "master", "key": "master",
                               "name": plan["scenes"][0]["name"],
                               "desc": plan["scenes"][0]["desc"], "status": "pending"}]
        master_st["total"] = 1
    store.mutate(sid, _apply)


def _item_update(sid: str, kind: str, idx: int, *, status: str,
                 error: str = "", score: float | None = None) -> None:
    def _apply(t: dict) -> None:
        st = t["pipeline"]["stages"][kind]
        it = st["items"][idx]
        it["status"] = status
        it["error"] = error[:160]
        if score is not None:
            it["score"] = score
        if status in ("done", "failed"):
            st["done"] += 1
    store.mutate(sid, _apply)


def _upsert_asset(sid: str, asset: dict) -> None:
    """同条目（item_id 优先，兼容旧 kind+name+season+gender）原位替换；
    被替换的旧图进版本历史（最多 _MAX_HISTORY 版）。

    注意：同名角色的男版/女版是两条不同素材，旧式键必须带 gender，
    否则后出的女版会把男版顶掉（只剩一张）。
    """
    def _apply(t: dict) -> None:
        kept, old = [], None
        for a in t["assets"]:
            same = ((asset.get("item_id") and a.get("item_id") == asset["item_id"])
                    or (a["kind"] == asset["kind"] and a["name"] == asset["name"]
                        and a.get("season") == asset.get("season")
                        and a.get("gender") == asset.get("gender")))
            if same and old is None:
                old = a
            else:
                kept.append(a)
        if old:
            history = old.get("history") or []
            history.insert(0, {k: old.get(k) for k in
                               ("path", "url", "seed", "tone", "created_at",
                                "prompt", "transparent")})
            del history[_MAX_HISTORY:]
            asset["history"] = history
        t["assets"] = kept + [asset]
    store.mutate(sid, _apply)


# ============================================================
#  各阶段执行体
# ============================================================

async def _run_stage(sid: str, kind: str, opts: dict | None = None) -> None:
    s = store.get_set(sid)
    if not s:
        return
    label = store.STAGE_LABELS[kind]
    store.append_log(sid, "info", f"开始「{label}」")

    if kind == "plan":
        await _stage_plan(sid, s)
    elif kind == "master":
        await _stage_master(sid)
    else:
        await _stage_assets(sid, kind, opts or {})

    _mark_stage(sid, kind, status="done")
    store.append_log(sid, "info", f"「{label}」完成")


async def _stage_plan(sid: str, s: dict) -> None:
    try:
        plan = await asyncio.to_thread(
            _plan_with_budget, s["idea"], s["scale"], _PLANNING_TIMEOUT)
    except Exception as exc:
        print(f"[gameassets] 策划降级规则模板：{exc}")
        store.append_log(sid, "warn", f"LLM 策划不可用（{exc}），降级规则模板")
        plan = await asyncio.to_thread(_rule_direct, s["idea"], s["scale"])
    _save_plan_and_items(sid, plan)
    counts = {k: len(plan[_PLAN_KEYS[k]]) for k in ASSET_KINDS}
    store.append_log(
        sid, "info",
        f"方案《{plan['title']}》就绪（{plan['planner']}）："
        + " ".join(f"{store.STAGE_LABELS[k]}×{counts[k]}" for k in ASSET_KINDS))


async def _stage_master(sid: str) -> None:
    s = store.get_set(sid)
    items = _reset_items(sid, "master")
    prompt, negative = master_prompt(s)
    _item_update(sid, "master", 0, status="running")
    w, h = _MASTER_SIZE
    last_err = ""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        if _canceled(s["id"]):
            raise _StageCanceled()
        try:
            rec = await _run_job(prompt=prompt, negative=negative,
                                 width=w, height=h, seed=-1, refs=None)
        except Exception as exc:
            last_err = str(exc)
            store.append_log(s["id"], "warn", f"基准图第 {attempt} 次生成失败：{exc}")
            continue
        q = quality.inspect(rec["path"], None)
        if q["pass"] or attempt == _MAX_ATTEMPTS:
            if not q["pass"]:
                store.append_log(s["id"], "warn",
                                 f"基准图 {attempt} 次尝试仍未达标：{'；'.join(q['reasons'])}（取最后一次）")
            palette = [c["hex"] for c in tonecheck.extract_palette(rec["path"])]

            def _save(t: dict, rec=rec, palette=palette, q=q, prompt=prompt,
                      items=items) -> None:
                t["master"] = {"path": rec["path"], "url": rec["url"],
                               "seed": rec.get("seed"), "desc": items[0]["desc"],
                               "size": [w, h], "palette": palette,
                               "quality": q["metrics"], "prompt": prompt}
            store.mutate(s["id"], _save)
            _item_update(sid, "master", 0, status="done")
            return
        store.append_log(s["id"], "warn",
                         f"基准图质量未达标（{'；'.join(q['reasons'])}），换 seed 重试 {attempt}/{_MAX_ATTEMPTS - 1}")
    _item_update(sid, "master", 0, status="failed", error=last_err or "重试仍失败")
    raise RuntimeError(f"基准图 {_MAX_ATTEMPTS} 次尝试均失败：{last_err}")


def _item_tag(item: dict) -> str:
    """日志/报错里的条目名：角色带性别，场景带季节。"""
    tag = item.get("name") or ""
    if item.get("season"):
        tag += f"·{SEASONS[item['season']]['name']}"
    if item.get("gender") in GENDERS:
        tag += f"·{GENDERS[item['gender']]['name']}版"
    return tag


def _pick_items(s: dict, kind: str, opts: dict) -> list[int]:
    """筛选重跑：返回要执行的条目下标。默认全部。"""
    st = s["pipeline"]["stages"][kind]
    f = opts.get("filter")
    if not f:
        return list(range(len(st["items"])))
    out = []
    for i, it in enumerate(st["items"]):
        if f == "failed" and it.get("status") == "failed":
            out.append(i)
        elif f == "tone" and it.get("status") == "done":
            asset = next((a for a in s["assets"] if a.get("item_id") == it.get("id")),
                         None)
            if asset and not (asset.get("tone") or {}).get("pass", True):
                out.append(i)
        elif f == "season" and it.get("season") == opts.get("season"):
            out.append(i)
        elif f == "gender" and it.get("gender") == opts.get("gender"):
            out.append(i)
    return out


async def _stage_assets(sid: str, kind: str, opts: dict) -> None:
    s = store.get_set(sid)
    if not s.get("master"):
        raise RuntimeError("基准图未就绪，请先生成基准图")
    master_path = s["master"]["path"]
    base_threshold = float(s.get("threshold") or tonecheck.DEFAULT_THRESHOLD)
    tmode = transparent_mode(s, kind)
    w, h = asset_size(s, kind)

    # 条目兜底：有方案但条目表空（历史遗留）→ 现场展开
    if not s["pipeline"]["stages"][kind]["items"]:
        def _fill(t: dict) -> None:
            items = build_stage_items(
                t["plan"], {k: t["pipeline"]["stages"][k]["items"]
                            for k in ASSET_KINDS})
            t["pipeline"]["stages"][kind]["items"] = items[kind]
            t["pipeline"]["stages"][kind]["total"] = len(items[kind])
        store.mutate(sid, _fill)
        s = store.get_set(sid)

    picked = _pick_items(s, kind, opts)
    if not picked:
        store.append_log(sid, "info", f"「{store.STAGE_LABELS[kind]}」筛选条件下无待跑条目")
        return
    target_ids = {s["pipeline"]["stages"][kind]["items"][i].get("id") for i in picked}
    _reset_items(sid, kind, only_ids=target_ids)
    s = store.get_set(sid)
    full = s["pipeline"]["stages"][kind]["items"]
    targets = [(i, it) for i, it in enumerate(full) if it.get("id") in target_ids]

    n_fail = 0
    for real_i, item in targets:
        if _canceled(sid):
            raise _StageCanceled()
        _item_update(sid, kind, real_i, status="running")
        name_tag = _item_tag(item)
        try:
            payload_item = {"kind": kind, "name": item["name"],
                            "desc": _clean(item["desc"]),
                            "season": item.get("season"), "tier_id": item.get("tier_id"),
                            "gender": item.get("gender")}
            prompt = asset_prompt(s, payload_item, transparent=tmode != "none",
                                  tileable=bool(item.get("tileable")))
            threshold = base_threshold * (_SEASON_THRESHOLD_FACTOR
                                          if item.get("season") else 1.0)
            asset = await _gen_with_gate(
                sid=sid, kind=kind, item=item, prompt=prompt,
                negative=asset_negative(s, kind), threshold=threshold,
                size=(w, h), tmode=tmode, master_path=master_path)
            _upsert_asset(sid, asset)
            _item_update(sid, kind, real_i, status="done",
                         score=asset["tone"]["score"])
            verdict = "✓" if asset["quality"]["pass"] else "✗" + "；".join(
                r.split("（")[0] for r in asset["quality"]["reasons"])
            store.append_log(sid, "info",
                             f"{store.STAGE_LABELS[kind]}「{name_tag}」完成 "
                             f"色调{asset['tone']['score']:.0f} {verdict}")
        except Exception as exc:
            n_fail += 1
            _item_update(sid, kind, real_i, status="failed", error=str(exc))
            store.append_log(sid, "error",
                             f"{store.STAGE_LABELS[kind]}「{name_tag}」失败：{exc}")
    if n_fail:
        raise RuntimeError(f"{n_fail}/{len(targets)} 张失败（可筛选重跑仅失败条目）")


async def _gen_with_gate(*, sid: str, kind: str, item: dict, prompt: str,
                         threshold: float, size: tuple[int, int], tmode: str,
                         master_path: str, seed: int = -1,
                         negative: str | None = None) -> dict:
    """生成 + 质量守门 + 自动重试 + 透明底后处理，产出素材记录。

    守门规则：黑图/无结构/低熵/跑色任一不达标 → 换 seed 重试；
    结构性缺陷（黑图等）重试后仍差 → 失败；仅跑色 → 入库但标记（用户可用筛选重跑）。
    """
    w, h = size
    neg = negative or _COMMON_NEG
    last_q, last_rec = None, None
    structural_fail = True
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        if _canceled(sid):
            raise _StageCanceled()
        rec = await _run_job(prompt=prompt, negative=neg,
                             width=w, height=h, seed=seed, refs=[master_path])
        last_rec, seed = rec, -1          # 首次用调用方 seed，后续换新 seed
        last_q = quality.inspect(rec["path"], master_path, threshold=threshold)
        structural = [r for r in last_q["reasons"] if not r.startswith("跑色")]
        if not structural:
            structural_fail = False
            if last_q["pass"]:
                break
            # 仅跑色：不再烧显存重试跑色（重跑大概率还是跑色，交给筛选重跑换措辞）
            break
        if attempt < _MAX_ATTEMPTS:
            store.append_log(sid, "warn",
                             f"「{item['name']}」质量未达标（{'；'.join(structural)}），"
                             f"换 seed 重试 {attempt}/{_MAX_ATTEMPTS - 1}")
    if last_rec is None:
        raise RuntimeError("生成失败（无有效返回）")

    path, transparent = last_rec["path"], None
    if tmode != "none":
        try:
            out = Path(path).with_name(Path(path).stem + "_alpha.png")
            rep = transmat.make_transparent(path, out, method=tmode)
            path, transparent = str(out), rep
        except Exception as exc:
            store.append_log(sid, "warn",
                             f"「{item['name']}」透明底处理失败（{exc}），保留原图")
    p = Path(path).resolve()
    try:
        rel = p.relative_to(settings.data_dir_path.resolve())
        url = f"/data/{rel.as_posix()}"
    except ValueError:
        url = f"/data/{p.name}"
    return {
        "id": new_id("ast"),
        "item_id": item.get("id"),
        "kind": kind, "kind_label": store.STAGE_LABELS[kind],
        "name": item["name"], "desc": item["desc"],
        "season": item.get("season"),
        "season_name": SEASONS[item["season"]]["name"] if item.get("season") else None,
        "gender": item.get("gender"),
        "gender_name": (GENDERS[item["gender"]]["name"]
                        if item.get("gender") in GENDERS else None),
        "tier": item.get("tier"), "tier_id": item.get("tier_id"),
        "path": path, "url": url, "seed": last_rec.get("seed"),
        "size": [w, h], "transparent": transparent,
        "tileable": bool(item.get("tileable")),
        "prompt": prompt,
        "quality": {"pass": last_q["pass"], "reasons": last_q["reasons"],
                    "metrics": last_q["metrics"], "attempts": attempt},
        "tone": last_q["tone"],
        "structural_fail": structural_fail,
        "created_at": store.now_iso(),
    }


# ============================================================
#  提示词组装（风格基因 + 时间背景 + 透明底/可平铺修饰）
# ============================================================

import re as _re

_PAREN_RE = _re.compile(r"[（(][^）)]*[）)]")


def _clean(text: str) -> str:
    """剥掉方案文案里的中文括号注记（如「（XX风格）」）——
    2.1 是文字渲染强模型，括号里的中文会被当成「要画到图上的字」整排画出来。"""
    return _PAREN_RE.sub("", str(text or "")).strip().strip("，,、 ")


def _style_block(s: dict) -> str:
    style = get_style(s["style"])
    preset = get_preset(s["time_preset"])
    user_style = str(s.get("desc") or "").strip()
    return (f"{user_style + ', ' if user_style else ''}"
            f"{style['prompt']}, {preset['prompt']}")


def master_prompt(s: dict) -> tuple[str, str]:
    style, preset = get_style(s["style"]), get_preset(s["time_preset"])
    scene = (s["plan"]["scenes"] or [{"name": "主场景", "desc": s["idea"]}])[0]
    prompt = (f"game concept art, environment establishing shot, "
              f"{_clean(scene['name'])} - {_clean(scene['desc'])}, {_style_block(s)}, "
              "rich details, no characters, no text")
    return prompt, f"{_COMMON_NEG}, {style['negative']}, {preset['negative']}"


_TILEABLE_HINT = ("seamless tileable texture, flat top-down perspective, even lighting, "
                  "no vignette, edges must align for infinite tiling")


def asset_prompt(s: dict, item: dict, *, transparent: bool = False,
                 tileable: bool = False) -> str:
    """六类素材统一组装。item: {kind,name,desc,season?,tier_id?,gender?}。

    结构：参考图锚点 → 该类固定模板 → 季节/等级/性别修饰 → 项目级补充词 → 收尾。
    性别词放在主体最前面（2.1 对前置主体词最敏感）。
    """
    kind = item["kind"]
    anchor = ("Using the reference image strictly as style, color palette and lighting "
              f"anchor (time of day: {get_preset(s['time_preset'])['name']}), ")
    bg = _ISOLATED_BG if transparent else "plain light neutral background"
    gender = GENDERS.get(item.get("gender"), {}).get("prompt", "")
    body = {
        "scene":    (f"game environment concept art: {_clean(item['name'])} - "
                     f"{_clean(item['desc'])}, "
                     + (f"{SEASONS[item['season']]['prompt']} season, "
                        if item.get("season") else "")
                     + ("full detailed environment, " + _TILEABLE_HINT
                        if tileable else "full detailed environment")),
        "character": (f"full body character design, single character, "
                      f"{gender + ', ' if gender else ''}front view, "
                      f"standing pose, whole body visible: {_clean(item['name'])} - "
                      f"{_clean(item['desc'])}, {bg}, centered"),
        "pet":      (f"cute game pet companion creature design, full body, side-front view: "
                     f"{_clean(item['name'])} - {_clean(item['desc'])}, {bg}, centered"),
        "weapon":   (f"game weapon design, single weapon, centered, three-quarter view: "
                     f"{_clean(item['name'])} - {_clean(item['desc'])}, "
                     + (f"{WEAPON_TIERS[item['tier_id']]['prompt']} quality tier, "
                        if item.get("tier_id") else "")
                     + bg),
        "building": (f"game architecture concept art, exterior view: "
                     f"{_clean(item['name'])} - {_clean(item['desc'])}, full structure visible"),
        "monster":  (f"game monster creature design, full body, menacing silhouette: "
                     f"{_clean(item['name'])} - {_clean(item['desc'])}, {bg}, centered"),
    }[kind]
    extra = _clean(prompt_extra(s, kind))          # 项目级补充（该类独有）
    if extra:
        body = f"{body}, {extra}"
    return f"{anchor}{body}. Keep style, palette and atmosphere identical to the reference"


def asset_negative(s: dict, kind: str) -> str:
    """负面词 = 通用 + 风格 + 时间背景 + 该类项目级补充。"""
    style, preset = get_style(s["style"]), get_preset(s["time_preset"])
    parts = [_COMMON_NEG, style["negative"], preset["negative"],
             prompt_negative(s, kind)]
    return ", ".join(p for p in parts if p)



# ============================================================
#  策划（LLM 带硬超时 + 取消；超时降级规则模板）
# ============================================================

def _plan_with_budget(idea: str, scale_id: str, budget: float) -> dict:
    result: dict = {}

    def _worker() -> None:
        try:
            result["plan"] = plan_from_idea(idea, scale_id)
        except Exception as exc:
            result["error"] = str(exc)

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout=budget)
    if th.is_alive():
        try:
            engine.text_cancel()
        except Exception:
            pass
        th.join(timeout=10)
        raise TimeoutError(f"LLM 策划超过 {int(budget)} 秒，降级规则模板")
    if "error" in result:
        raise RuntimeError(result["error"])
    return result["plan"]


def _rule_direct(idea: str, scale_id: str) -> dict:
    """绕过 LLM 直接走规则模板（策划超时后的快速路径，永不超时）。"""
    from .planner import SCALES, _rule_plan, _normalize
    return _normalize(_rule_plan(idea), SCALES.get(scale_id, SCALES["standard"]))


# ============================================================
#  引擎调用（refs 支持多参考图；先入库再同步生成）
# ============================================================

async def _run_job(*, prompt: str, negative: str, width: int, height: int,
                   seed: int, refs: list[str] | None = None) -> dict:
    payload = {
        "job_id": new_id("job"),
        "prompt": prompt,
        "negative_prompt": negative,
        "width": width,
        "height": height,
        "steps": settings.default_steps,
        "guidance_scale": settings.default_guidance,
        "seed": seed,
        "count": 1,
        "channel": "auto",
        "output_subdir": "gameassets",
        "mode": "txt2img",
        "gameassets": {"kind": "pipeline"},
    }
    use_refs = []
    for rp in (refs or []):
        p = Path(rp)
        if not p.is_absolute():
            p = Path(".") / p
        use_refs.append(f"data:image/png;base64,{base64.b64encode(p.read_bytes()).decode()}")
    if use_refs:
        payload.update({
            "mode": "img2img",
            "fusion": True,
            "ref_images": use_refs,
            "negative_prompt": "",
            "guidance_scale": 1.0,
            "strength": 1.0,
        })
    repo.create_job(job_id=payload["job_id"], prompt=prompt, negative=negative,
                    params=payload, seed=seed, count=1,
                    mode="img2img" if use_refs else "txt2img")
    if settings.engine_mode == "local":
        engine.vram_guard(1.0)
    job = await asyncio.wait_for(
        engine.generate_sync(payload),
        timeout=settings.infer_timeout_seconds,
    )
    if job.get("status") != "succeeded":
        raise RuntimeError(job.get("error") or "生成失败")
    imgs = repo.images_by_job(payload["job_id"])
    if not imgs:
        raise RuntimeError("引擎没有返回图片")
    rec = dict(imgs[0])
    p = Path(rec["path"]).resolve()
    try:
        rel = p.relative_to(settings.data_dir_path.resolve())
        rec["url"] = f"/data/{rel.as_posix()}"
    except ValueError:
        rec["url"] = f"/data/{p.name}"
    return rec


# ============================================================
#  多候选（单张精修）：生成 N 张候选（不提交），选优后 commit
# ============================================================

async def gen_variants(*, sid: str, aid: str, prompt: str | None = None,
                       seed: int = -1, n: int = 1,
                       use_self_ref: bool = True) -> list[dict]:
    """为已有素材生成候选图（不替换正式版）。角色/宠物等默认带自身参考图，
    保持同一角色多姿态一致。候选经质量检测，但由用户挑。"""
    s = store.get_set(sid)
    if not s:
        raise ValueError("项目不存在")
    if not s.get("master"):
        raise ValueError("基准图缺失")
    asset = next((a for a in s["assets"] if a["id"] == aid), None)
    if not asset:
        raise ValueError("素材不存在")
    n = max(1, min(_MAX_CANDIDATES, int(n)))
    kind = asset["kind"]
    tmode = transparent_mode(s, kind)
    w, h = tuple(asset.get("size") or asset_size(s, kind))
    threshold = float(s.get("threshold") or tonecheck.DEFAULT_THRESHOLD) * (
        _SEASON_THRESHOLD_FACTOR if asset.get("season") else 1.0)
    final_prompt = prompt or asset["prompt"]
    refs = [s["master"]["path"]]
    if use_self_ref and asset.get("path"):
        refs.append(asset["path"])
    variants = []
    for i in range(n):
        if _canceled(sid):
            break
        rec = await _run_job(prompt=final_prompt, negative=asset_negative(s, kind),
                             width=w, height=h, seed=seed, refs=refs)
        q = quality.inspect(rec["path"], s["master"]["path"], threshold=threshold)
        v = {"id": new_id("var"), "path": rec["path"], "url": rec["url"],
             "seed": rec.get("seed"), "quality": q, "prompt": final_prompt,
             "created_at": store.now_iso()}
        variants.append(v)
        store.append_log(sid, "info",
                         f"候选 {i + 1}/{n}（{asset['name']}）完成 "
                         f"色调{q['tone']['score']:.0f} "
                         f"{'✓' if q['pass'] else '✗' + '；'.join(r.split('（')[0] for r in q['reasons'])}")
        seed = -1
    return variants


def commit_variant(*, sid: str, aid: str, variant: dict,
                   transparent: bool | None = None) -> dict:
    """把候选图提交为正式版：旧正式版进版本历史；候选图转透明底（按类）。"""
    s = store.get_set(sid)
    if not s:
        raise ValueError("项目不存在")
    idx = next((i for i, a in enumerate(s["assets"]) if a["id"] == aid), None)
    if idx is None:
        raise ValueError("素材不存在")
    old = s["assets"][idx]
    kind = old["kind"]
    tmode = transparent_mode(s, kind) if transparent is None else (
        "none" if not transparent else "chroma")
    path, transparent = variant["path"], None
    if tmode != "none":
        try:
            out = Path(path).with_name(Path(path).stem + "_alpha.png")
            rep = transmat.make_transparent(path, out, method=tmode)
            path, transparent = str(out), rep
        except Exception as exc:
            store.append_log(sid, "warn",
                             f"「{old['name']}」透明底处理失败（{exc}），保留原图")
    p = Path(path).resolve()
    try:
        rel = p.relative_to(settings.data_dir_path.resolve())
        url = f"/data/{rel.as_posix()}"
    except ValueError:
        url = f"/data/{p.name}"
    history = old.get("history") or []
    history.insert(0, {k: old.get(k) for k in
                       ("path", "url", "seed", "tone", "created_at",
                        "prompt", "transparent")})
    del history[_MAX_HISTORY:]
    new_asset = {**old,
                 "path": path, "url": url, "seed": variant.get("seed"),
                 "prompt": variant.get("prompt", old.get("prompt")),
                 "transparent": transparent,
                 "quality": {"pass": variant["quality"]["pass"],
                             "reasons": variant["quality"]["reasons"],
                             "metrics": variant["quality"]["metrics"],
                             "attempts": 1},
                 "tone": variant["quality"]["tone"],
                 "history": history, "rerun_of": aid,
                 "created_at": store.now_iso()}

    def _apply(t: dict) -> None:
        t["assets"][idx] = new_asset
    store.mutate(sid, _apply)
    store.append_log(sid, "info", f"「{old['name']}」已更新（候选提交，旧版进历史）")
    return new_asset


def overall_status(s: dict) -> str:
    """列表页聚合状态：有在跑=running；有失败=done_with_errors；全齐=done。"""
    stages = s["pipeline"]["stages"]
    if any(st["status"] in ("queued", "running") for st in stages.values()):
        return "running"
    statuses = [st["status"] for st in stages.values()]
    if "failed" in statuses:
        return "done_with_errors" if "done" in statuses else "failed"
    if all(stages[k]["status"] == "done" for k in ("plan", "master", *ASSET_KINDS)):
        return "done"
    if stages["plan"]["status"] == "done":
        return "partial"
    return "idle"
