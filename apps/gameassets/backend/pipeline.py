# -*- coding: utf-8 -*-
"""游戏素材流水线 5.0：分类独立生成 + 异常透明 + 质量守门 + 可控精修。

八个子阶段（store.STAGES）各自独立入队、独立进度、独立失败：
    plan(策划) → master(基准图) → scene / character / pet / weapon / building / monster

所有阶段进同一条 FIFO 串行工作队列（保显存顺序），类与类之间互不拖累。
"all" 是编排入口：按依赖顺序串起来，已完成（done）的跳过 = 续做语义。

5.0 新增：
  - 质量守门（quality.py）：黑图/纯色/低熵/跑色 四重检测，不过关自动换 seed 重试
  - 透明底（transmat.py）：按类指定 chroma 色键 / rembg，出图后即转 RGBA
  - 按类画幅（presets.ASPECTS）：角色 2:3、武器/宠物 1:1、建筑 4:3；
    场景按「用途」、怪物按「等级」逐条定画幅——每条都有写死的理由，不再是拍脑袋
  - 场景 ↔ 怪物配属：每只野怪写明出没场景，提示词里带环境上下文，可按场景筛选重跑
  - 筛选重跑：enqueue 带 opts（failed / tone / season），只重跑命中的条目
  - 阶段取消：cancel_stage()，逐张检查取消标志，已出的素材保留
  - 多候选：gen_variants() 一次出 N 张候选，选优提交（提交时旧图进版本历史）
  - 多参考图：_run_job 接受 refs 列表（基准图 + 角色当前图），换装/多姿态一致性

11.0 新增（借力 awesome-gamedev-agent-skills 的 create-game-assets 方法论）：
  - 美术方向契约（artdir.py）：八轴视觉系统，规则推导 + 用户可改，逐张注入提示词
  - 提示词八段块：ROLE → SUBJECT → VIEW → ART DIRECTION → GAME-SCALE READ
    → TECHNICAL OUTPUT → LOCKS → EXCLUDE；不变块逐张复用 = 家族一致性
  - alpha 守门（quality.alpha_check）：假透明（RGBA 全不透明）/抠图丢失/裁边检测
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

from . import anim, artdir, quality, store, tonecheck, transmat
from .planner import plan_from_idea
from .presets import (ASPECTS, get_preset, asset_size, transparent_mode,
                      SEASONS, WEAPON_TIERS, ASSET_KINDS, build_stage_items,
                      GENDERS, prompt_extra, prompt_negative,
                      MONSTER_TIERS, MONSTER_TIER_ORDER, monster_tier,
                      SCENE_USAGES, scene_usage,
                      ANIM_KINDS, ANIM_LEVELS, ANIM_FRAME_SIZE, VIEW_LOCK,
                      anim_level, anim_spec,
                      CLIPS, CLIP_ORDER, DIRECTIONS)
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
        if opts.get("filter") == "tier" and opts.get("tier") not in MONSTER_TIERS:
            raise ValueError(f"未知怪物等级：{opts.get('tier')}")
        if opts.get("filter") == "zone" and not str(opts.get("zone") or "").strip():
            raise ValueError("按场景筛选需要 zone（场景名）")

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
        elif f == "tier" and opts.get("tier") in MONSTER_TIERS:
            suffix = f"（仅 {MONSTER_TIERS[opts['tier']]['name']}级）"
        elif f == "zone" and opts.get("zone"):
            suffix = f"（仅「{opts['zone']}」场景）"
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
    """同条目（item_id 优先，兼容旧 kind+name+season+gender+tier）原位替换；
    被替换的旧图进版本历史（最多 _MAX_HISTORY 版）。

    注意：同名角色的男版/女版、同名怪的不同等级都是不同素材，
    旧式键必须带 gender + tier_id，否则同类后出的那张会把前一张顶掉。
    """
    def _apply(t: dict) -> None:
        kept, old = [], None
        for a in t["assets"]:
            same = ((asset.get("item_id") and a.get("item_id") == asset["item_id"])
                    or (a["kind"] == asset["kind"] and a["name"] == asset["name"]
                        and a.get("season") == asset.get("season")
                        and a.get("gender") == asset.get("gender")
                        and a.get("tier_id") == asset.get("tier_id")))
            if same and old is None:
                old = a
            else:
                kept.append(a)
        if old and old.get("id") != asset["id"]:
            # 同 id（12.1 动画分步生成的进度记录）直接顶掉，不进版本历史；
            # 真重跑（新 id）才把旧图存档。
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
    # 美术方向契约：方案落定后按风格+世界观规则推导八轴（用户手填过则不覆盖）
    def _seed_ad(t: dict) -> None:
        if not t.get("artdir") and not t.get("artdir_edited"):
            t["artdir"] = artdir.derive(t)
    store.mutate(sid, _seed_ad)
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


def _item_tag(item: dict, kind: str | None = None) -> str:
    """日志/报错里的条目名：场景带季节、角色带性别、野怪带等级。"""
    tag = item.get("name") or ""
    if item.get("season"):
        tag += f"·{SEASONS[item['season']]['name']}"
    if item.get("gender") in GENDERS:
        tag += f"·{GENDERS[item['gender']]['name']}版"
    if kind == "monster" and item.get("tier"):
        tag += f"·{item['tier']}"
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
        elif f == "tier" and it.get("tier_id") == opts.get("tier"):
            out.append(i)
        elif f == "zone" and it.get("scene") == opts.get("zone"):
            out.append(i)
    return out


async def _stage_assets(sid: str, kind: str, opts: dict) -> None:
    s = store.get_set(sid)
    if not s.get("master"):
        raise RuntimeError("基准图未就绪，请先生成基准图")
    master_path = s["master"]["path"]
    base_threshold = float(s.get("threshold") or tonecheck.DEFAULT_THRESHOLD)
    tmode = transparent_mode(s, kind)

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
        name_tag = _item_tag(item, kind)
        try:
            payload_item = {"kind": kind, "name": item["name"],
                            "desc": _clean(item["desc"]),
                            "season": item.get("season"), "tier_id": item.get("tier_id"),
                            "gender": item.get("gender"), "usage": item.get("usage"),
                            "scene": item.get("scene")}
            prompt = asset_prompt(s, payload_item, transparent=tmode != "none",
                                  tileable=bool(item.get("tileable")))
            threshold = base_threshold * (_SEASON_THRESHOLD_FACTOR
                                          if item.get("season") else 1.0)
            w, h = asset_size(s, kind, item)      # 场景按用途、怪物按等级逐条定画幅
            lv = anim_level(s, item)
            if kind in ANIM_KINDS and lv != "off":
                prev = next((a for a in s["assets"]
                             if a.get("item_id") == item.get("id") and a.get("anim")),
                            None)                     # 上次记录：增量续跑的依据
                asset = await _gen_anim_asset(
                    sid=sid, s=s, kind=kind, item=item, payload=payload_item,
                    master_path=master_path, tmode=tmode, threshold=threshold,
                    size=(w, h), level=lv, prev=prev)
            else:
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


def _url_of(path: str | Path) -> str:
    """本地绝对路径 → /data/... 静态地址。"""
    p = Path(path).resolve()
    try:
        rel = p.relative_to(settings.data_dir_path.resolve())
        return f"/data/{rel.as_posix()}"
    except ValueError:
        return f"/data/{p.name}"


def _alpha_to_ref(path: str | Path) -> str:
    """抠好的透明帧不能直接当图生图参考（透明区会被当成白色），
    压到中性灰底上再喂给引擎，颜色与轮廓才不失真。"""
    src = Path(path)
    dst = src.with_name(src.stem + "_ref.png")
    if not dst.is_file():
        try:
            from PIL import Image as _Im
            im = _Im.open(src).convert("RGBA")
            bg = _Im.new("RGB", im.size, (208, 210, 214))
            bg.paste(im, (0, 0), im)
            bg.save(dst)
        except Exception:
            return str(src)
    return str(dst)


async def _gen_design_sheets(*, sid: str, s: dict, kind: str, item: dict,
                             payload: dict, master_path: str, spec: dict,
                             out_dir: Path, aid: str,
                             prev_design: dict | None) -> dict:
    """第一层：身份卡（多栏 turnaround sheet）。

    这是 13.0 的地基。12.x 每帧都从零采样，40 帧是 40 次独立掷骰子，
    模型的概率性直接把「同一个人」这件事交给了运气；守门又只量黑图/跑色，
    量不出「不像」。身份卡把这件事变成一次性的：先画一张「同一个角色的
    若干朝向并排」，后面每一帧都以这一栏当参考图，身份由参考图焊住。

    复用：档位一致 + 栏数一致 + 文件都还在 → 直接用上次的，不再出图。
    """
    ta = spec["turnaround"]
    sheets_meta: list[dict] = []
    columns: dict[str, dict] = {}
    if not ta.get("enabled"):
        return {"level": spec["id"], "sheets": [], "columns": {}}

    prev = prev_design or {}
    reuse = (prev.get("level") == spec["id"]
             and len(prev.get("sheets") or []) == ta["count"])

    def _exists(p: str | None) -> bool:
        if not p:
            return False
        q = Path(p)
        if not q.is_absolute():
            q = Path(".") / q
        return q.is_file()

    if reuse:
        for sm in prev.get("sheets") or []:
            if not _exists(sm.get("path")):
                reuse = False
                break
    if reuse:
        for d, cm in (prev.get("columns") or {}).items():
            if not _exists(cm.get("path")):
                reuse = False
                break

    if reuse:
        store.append_log(sid, "info",
                         f"{store.STAGE_LABELS[kind]}「{_item_tag(item, kind)}」"
                         f"复用已有身份卡（{len(prev.get('sheets') or [])} 张）")
        return {"level": spec["id"], "sheets": prev.get("sheets") or [],
                "columns": prev.get("columns") or {}, "reused": True}

    neg = (asset_negative(s, kind) + ", multiple different characters, "
           "inconsistent costume between columns, inconsistent colors, "
           "labels, arrows, numbered columns, border, frame")
    for si, sheet in enumerate(ta["sheets"]):
        if _canceled(sid):
            raise _StageCanceled()
        dirs = sheet["dirs"]
        w, h = sheet["size"]
        prompt = design_prompt(s, item, dirs, si, ta["count"])
        rec = await _gen_with_gate(
            sid=sid, kind=kind, item=item, prompt=prompt, negative=neg,
            threshold=float(s.get("threshold") or tonecheck.DEFAULT_THRESHOLD),
            size=(w, h), tmode="none", master_path=master_path, max_attempts=2)
        raw = Path(rec["path"])
        # 落到项目自己的目录里（引擎出的图在 outputs/gameassets 下按时间戳命名，
        # 那张是「原始出图」；身份卡要按素材 id 命名，便于导出与复用校验）
        dst = out_dir / f"{aid}_design{si}.png"
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            from PIL import Image as _Im
            _Im.open(raw).convert("RGB").save(dst)
        except Exception:
            dst = raw
        sheets_meta.append({"index": si, "path": str(dst), "url": _url_of(dst),
                            "size": [w, h], "dirs": dirs, "cols": len(dirs),
                            "source": str(raw)})
        for ci, d in enumerate(dirs):
            cp = anim.crop_column(dst, len(dirs), ci,
                                  out_dir / f"{aid}_col_{d}.png")
            columns[d] = {"sheet": si, "col": ci, "dir": d,
                          "path": str(cp), "url": _url_of(cp)}
        store.append_log(
            sid, "info",
            f"{store.STAGE_LABELS[kind]}「{_item_tag(item, kind)}」身份卡 "
            f"{si + 1}/{ta['count']} 完成：{'/'.join(DIRECTIONS[d]['name'] for d in dirs)}"
            f"（{w}×{h}）")
    return {"level": spec["id"], "sheets": sheets_meta, "columns": columns,
            "reused": False}


async def _gen_anim_asset(*, sid: str, s: dict, kind: str, item: dict, payload: dict,
                          master_path: str, tmode: str, threshold: float,
                          size: tuple[int, int], level: str,
                          prev: dict | None = None) -> dict:
    """角色 / 宠物：三层分布出帧（13.0），对齐后拼成精灵图集。

    ① 身份卡：一张图并排画同一个角色的多个朝向（1~2 次出图，全朝向共用）；
    ② 关键姿态：每个朝向只出站立 2 / 行走 4 / 奔跑 4 帧，参考图 = 基准图
       + 身份卡对应那一栏，图生图把身份焊住；
    ③ 插值补帧：接触帧压低、过渡帧抬高，整帧垂直位移补出中间帧，不烧显存。

    对比 12.x 的逐帧文生图：lite 档 40 帧从 40 次出图降到 21 次，且「像不像
    同一个人」从概率问题变成由参考图决定。分步生成与增量续跑沿用 12.1：
    逐朝向落库，档位/方法/图集元数据都对得上就跳过。
    """
    spec, neg = anim_spec(level), asset_negative(s, kind)
    cell = tuple(VIEW_LOCK["cell"])
    anchor = tuple(VIEW_LOCK["anchor"])
    fw, fh = ANIM_FRAME_SIZE
    out_dir = settings.data_dir_path / "outputs" / "gameassets" / "anim" / sid
    aid = (prev or {}).get("id") or new_id("ast")
    tag = f"{store.STAGE_LABELS[kind]}「{_item_tag(item, kind)}」"

    prev_anim = (prev or {}).get("anim") or {}
    prev_banks: dict[str, dict] = prev_anim.get("banks") or {}
    level_same = prev_anim.get("level") == spec["id"]
    exp_clips = {c: n for c, n in spec["clips"].items() if n}
    ta = spec["turnaround"]

    def _bank_ok(b: dict | None) -> bool:
        """旧朝向图集能否直接复用：档位没变、方法一致、文件还在、行列与规格一致。"""
        if not (b and level_same):
            return False
        if b.get("method") != "keypose":        # 12.x 的逐帧图集不再复用
            return False
        p = Path(b.get("path") or "")
        if not p.is_absolute():
            p = Path(".") / p
        meta = b.get("clips") or {}
        return (p.is_file() and b.get("cols") == max(exp_clips.values())
                and set(meta) == set(exp_clips)
                and all(meta.get(c, {}).get("count") == n for c, n in exp_clips.items()))

    banks: dict[str, dict] = {}
    cover: Path | None = None
    scores: list[float] = []
    reasons: list[str] = []
    frames_done = 0
    keys_done = 0

    # ---------- 第一层：身份卡 ----------
    # 全部朝向都已有可复用图集时连身份卡也不必再出（真续跑：零出图）
    if all(_bank_ok(prev_banks.get(d)) for d in spec["dirs"]):
        design = prev_anim.get("design") or {"level": spec["id"], "sheets": [],
                                             "columns": {}}
    else:
        design = await _gen_design_sheets(
            sid=sid, s=s, kind=kind, item=item, payload=payload,
            master_path=master_path, spec=spec, out_dir=out_dir, aid=aid,
            prev_design=prev_anim.get("design"))

    def _record() -> dict:
        """当前进度的素材记录（半成品 / 成品共用，anim.expected 标总数）。"""
        return {
            "id": aid,
            "item_id": item.get("id"),
            "kind": kind, "kind_label": store.STAGE_LABELS[kind],
            "name": item["name"], "desc": item["desc"],
            "season": None, "season_name": None,
            "gender": item.get("gender"),
            "gender_name": (GENDERS[item["gender"]]["name"]
                            if item.get("gender") in GENDERS else None),
            "tier": item.get("tier"), "tier_id": item.get("tier_id"),
            "usage": None, "usage_name": None, "usage_reason": None, "scene": None,
            "path": str(cover) if cover else None,
            "url": _url_of(cover) if cover else None,
            "seed": None,
            "size": list(cell), "transparent": {"method": tmode},
            "tileable": False,
            "prompt": asset_prompt(s, payload, transparent=tmode != "none"),
            "quality": {"pass": not reasons, "reasons": reasons[:6],
                        "metrics": {}, "attempts": 1},
            "tone": {"score": min(scores) if scores else 0.0,
                     "threshold": threshold, "pass": not reasons},
            "structural_fail": bool(reasons),
            "frame_size": [fw, fh],
            "anim": {
                "level": spec["id"], "level_name": spec["name"],
                "method": "turnaround",          # 三层分布（身份卡 → 关键姿态 → 插值）
                "cell": list(cell), "anchor": list(anchor),
                "dirs": spec["dirs"], "mirror": spec["mirror"],
                "clips": {cid: {"name": CLIPS[cid]["name"], "fps": CLIPS[cid]["fps"],
                                "count": spec["clips"][cid]}
                          for cid in CLIP_ORDER if spec["clips"].get(cid)},
                "banks": banks,
                "frames_total": frames_done,
                "expected": spec["frames_total"],
                # 三层分布的账：身份卡 / 关键姿态 / 插值，前端与导出都用得上
                "design": design,
                "keys": spec["keys"],
                "tweens": {c: {i: {"from": src, "dy": dy}
                               for i, (src, dy) in (spec["tweens"].get(c) or {}).items()}
                           for c in spec["tweens"]},
                "key_frames": keys_done,
                "key_expected": spec["keys_total"],
                "interp_frames": frames_done - keys_done,
                "calls_total": spec["calls_total"],
                "view": anim.view_meta(),
            },
            "created_at": (prev or {}).get("created_at") or store.now_iso(),
        }

    for dir_id in spec["dirs"]:
        if _bank_ok(prev_banks.get(dir_id)):
            banks[dir_id] = prev_banks[dir_id]
            frames_done += spec["per_dir"]
            keys_done += spec["keys_per_dir"]
            store.append_log(sid, "info",
                             f"{tag} 朝向「{DIRECTIONS[dir_id]['name']}」已有帧图，"
                             f"跳过生成（帧 {frames_done}/{spec['frames_total']}）")
            continue

        # 该朝向的参考栏：身份卡实拍栏，或（standard 的 ne）从邻近栏 + 视角词派生
        where = ta["where"].get(dir_id)
        view_phrase = ""
        if where:
            cm = design["columns"].get(dir_id) or {}
        else:
            dv = ta["derived"].get(dir_id) or {}
            cm = design["columns"].get(dv.get("from", "")) or {}
            view_phrase = dv.get("view", "")
        col_ref = cm.get("path")

        keys: dict[str, dict[int, str]] = {}
        for clip in CLIP_ORDER:
            count = spec["clips"].get(clip) or 0
            idxs = spec["keys"].get(clip) or []
            if not count or not idxs:
                continue
            keys[clip] = {}
            for i in idxs:
                if _canceled(sid):
                    raise _StageCanceled()
                prompt = asset_prompt(
                    s, payload, transparent=tmode != "none",
                    anim_tail=anim.key_suffix(clip, i, count, dir_id, view_phrase))
                rec = await _gen_with_gate(
                    sid=sid, kind=kind, item=item, prompt=prompt, negative=neg,
                    threshold=threshold, size=(fw, fh), tmode=tmode,
                    master_path=master_path, max_attempts=2,
                    extra_refs=[col_ref] if col_ref else None)
                keys[clip][i] = rec["path"]
                scores.append(float(rec["tone"]["score"]))
                if not rec["quality"]["pass"]:
                    reasons += [r for r in rec["quality"]["reasons"]
                                if r not in reasons]
                keys_done += 1
        frames = anim.build_frames_from_keys(keys, spec["tweens"], spec["clips"],
                                             cell, anchor)
        n_frames = sum(len(v) for v in frames.values())
        bank_path = out_dir / f"{aid}_{dir_id}.png"
        meta = anim.save_bank(frames, bank_path, cell)
        banks[dir_id] = {**meta, "dir": dir_id, "method": "keypose",
                         "dir_name": DIRECTIONS[dir_id]["name"],
                         # 这一向真出了几张、补了几张（导出/前端直接显示）
                         "keys": {c: len(v) for c, v in keys.items()},
                         "interp": {c: spec["clips"][c] - len(keys.get(c) or {})
                                    for c in keys},
                         "path": str(bank_path), "url": _url_of(bank_path)}
        if cover is None:
            cover = out_dir / f"{aid}_cover.png"
            frames["idle"][0].save(cover)
        frames_done += n_frames
        store.append_log(
            sid, "info",
            f"{tag} 朝向「{DIRECTIONS[dir_id]['name']}」完成 "
            f"（关键姿态 {spec['keys_per_dir']} 张 + 插值 "
            f"{n_frames - spec['keys_per_dir']} 帧，共 {n_frames} 帧；"
            f"整体 {frames_done}/{spec['frames_total']} 帧）")
        _upsert_anim_progress(sid, _record())          # 每朝向即落库，失败不报废

    if cover is None:
        # 全部朝向都走增量复用：复用旧封面；连封面都没了就从图集裁站立首帧
        old_cover = (prev or {}).get("path")
        if old_cover and Path(old_cover).is_file():
            cover = Path(old_cover)
        else:
            from PIL import Image as _Im
            b0 = banks[spec["dirs"][0]]
            bp = Path(b0["path"])
            sheet0 = _Im.open(bp).convert("RGBA")
            cover = out_dir / f"{aid}_cover.png"
            sheet0.crop((0, 0, cell[0], cell[1])).save(cover)
    return _record()


def _upsert_anim_progress(sid: str, asset: dict) -> None:
    """动画增量进度：按 id 原位替换，不进版本历史（半成品不值得占历史位）。"""
    def _apply(t: dict) -> None:
        t["assets"] = [a for a in t["assets"] if a.get("id") != asset["id"]]
        t["assets"].append(asset)
    store.mutate(sid, _apply)


async def _gen_with_gate(*, sid: str, kind: str, item: dict, prompt: str,
                         threshold: float, size: tuple[int, int], tmode: str,
                         master_path: str, seed: int = -1,
                         negative: str | None = None,
                         extra_refs: list[str] | None = None,
                         max_attempts: int = _MAX_ATTEMPTS) -> dict:
    """生成 + 质量守门 + 自动重试 + 透明底后处理，产出素材记录。

    守门规则：黑图/无结构/低熵/跑色任一不达标 → 换 seed 重试；
    结构性缺陷（黑图等）重试后仍差 → 失败；仅跑色 → 入库但标记（用户可用筛选重跑）。
    动画帧走 max_attempts=2：一个角色几十帧，单帧重试 3 次太拖；坏一帧
    还有整段（朝向）级续跑兜底。
    """
    w, h = size
    neg = negative or _COMMON_NEG
    last_q, last_rec = None, None
    structural_fail = True
    for attempt in range(1, max_attempts + 1):
        if _canceled(sid):
            raise _StageCanceled()
        rec = await _run_job(prompt=prompt, negative=neg,
                             width=w, height=h, seed=seed,
                             refs=[master_path] + list(extra_refs or []))
        last_rec, seed = rec, -1          # 首次用调用方 seed，后续换新 seed
        last_q = quality.inspect(rec["path"], master_path, threshold=threshold)
        structural = [r for r in last_q["reasons"] if not r.startswith("跑色")]
        if not structural:
            structural_fail = False
            if last_q["pass"]:
                break
            # 仅跑色：不再烧显存重试跑色（重跑大概率还是跑色，交给筛选重跑换措辞）
            break
        if attempt < max_attempts:
            store.append_log(sid, "warn",
                             f"「{item['name']}」质量未达标（{'；'.join(structural)}），"
                             f"换 seed 重试 {attempt}/{max_attempts - 1}")
    if last_rec is None:
        raise RuntimeError("生成失败（无有效返回）")

    path, transparent = last_rec["path"], None
    alpha_q: dict | None = None
    if tmode != "none":
        try:
            out = Path(path).with_name(Path(path).stem + "_alpha.png")
            rep = transmat.matte(path, out, method=tmode)   # rembg 优先，退化自动回落
            path, transparent = str(out), rep
            if rep.get("fallback"):
                store.append_log(sid, "warn",
                                 f"「{item['name']}」AI 抠图回落色键：{rep.get('rembg_error', '')}")
        except Exception as exc:
            store.append_log(sid, "warn",
                             f"「{item['name']}」透明底处理失败（{exc}），保留原图")
        # alpha 守门（11.0）：假透明/没通道/裁边 —— 烤死背景和裁边素材不许悄悄入库
        try:
            alpha_q = quality.alpha_check(path)
        except Exception:
            alpha_q = None
        if alpha_q and alpha_q["problems"]:
            msgs = {"fake_trans": "假透明（全图无一透明像素，抠图可能失败）",
                    "no_alpha": "没有透明通道",
                    "edge_touch": "主体贴满画框（疑似裁边）"}
            for code in alpha_q["problems"]:
                store.append_log(sid, "warn", f"「{item['name']}」{msgs.get(code, code)}")
            last_q["reasons"] = last_q["reasons"] + [
                msgs.get(c, c) for c in alpha_q["problems"] if c != "edge_touch"]
            last_q["pass"] = False
            structural_fail = True
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
        "usage": item.get("usage"),
        "usage_name": (SCENE_USAGES[item["usage"]]["name"]
                       if item.get("usage") in SCENE_USAGES else None),
        "usage_reason": (SCENE_USAGES[item["usage"]]["reason"]
                         if kind == "scene" and item.get("usage") in SCENE_USAGES else None),
        "scene": item.get("scene"),
        "path": path, "url": url, "seed": last_rec.get("seed"),
        "size": [w, h], "transparent": transparent,
        "alpha_qa": alpha_q,
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


def _artdir_block(s: dict) -> str:
    """美术方向契约 → 提示词不变块（八轴；空契约返回空串）。"""
    return artdir.prompt_block(s.get("artdir"))


def master_prompt(s: dict) -> tuple[str, str]:
    style, preset = get_style(s["style"]), get_preset(s["time_preset"])
    scene = (s["plan"]["scenes"] or [{"name": "主场景", "desc": s["idea"]}])[0]
    ad = _artdir_block(s)
    ad_tail = f", {ad}" if ad else ""
    prompt = (f"game concept art, environment establishing shot, "
              f"{_clean(scene['name'])} - {_clean(scene['desc'])}, {_style_block(s)}"
              f"{ad_tail}, "
              "rich details, no characters, no text")
    return prompt, f"{_COMMON_NEG}, {style['negative']}, {preset['negative']}"


_TILEABLE_HINT = ("seamless tileable texture, flat top-down perspective, even lighting, "
                  "no vignette, edges must align for infinite tiling")

# 场景用途 → 构图要求。尺寸定了还得让模型知道「为什么这么大」，
# 否则 9:4 的横幅里它照样给你画个正方构图再被裁掉。
_SCENE_HINT: dict[str, str] = {
    "battle": ("wide establishing shot, low horizon, main structure on the center axis, "
               "left and right thirds kept open for camera panning"),
    "tile": _TILEABLE_HINT,
    "portrait": ("tall vertical composition, elements stacked upward, "
                 "top and bottom bands kept simple for UI overlays"),
    "banner": ("ultra wide panoramic strip, very low horizon line, "
               "open sky filling the upper half for title text"),
    "iso": ("isometric 45-degree view, square playable ground filling the frame, "
            "crisp grid-friendly edges"),
}


def asset_prompt(s: dict, item: dict, *, transparent: bool = False,
                 tileable: bool = False, anim_tail: str = "") -> str:
    """六类素材统一组装——八段块结构（借力 create-game-assets 的提示词骨架）：

        ROLE/PURPOSE   生产用途（这张图是干嘛的）
        SUBJECT        主体（名字 + 视觉描述 + 性别/季节/等级/环境上下文）
        VIEW           视图锁（2.5D 45° / 场景自带取景词）
        ART DIRECTION  不变块：风格基因 + 时间背景 + 八轴美术契约
        GAME-SCALE READ 游戏尺度可读性（剪影/锚点要求）
        TECHNICAL OUTPUT 技术输出（画幅用途/底色要求/动画帧后缀）
        LOCKS          不变块：以参考图锚定，风格色板光照不许漂
        EXCLUDE        不变块：禁字/水印/边框/裁边

    不变块逐张复用是家族一致性的关键；可变块只有 SUBJECT/VIEW/TECHNICAL。
    性别词放在主体最前面（2.1 对前置主体词最敏感）。
    """
    kind = item["kind"]
    gender = GENDERS.get(item.get("gender"), {}).get("prompt", "")
    who = f"{_clean(item['name'])} - {_clean(item['desc'])}"
    bg = _ISOLATED_BG if transparent else "plain light neutral background"

    if kind == "scene":
        usage = scene_usage(item) or ("tile" if tileable else "battle")
        role = "game environment concept art, production background"
        subject = (who
                   + (f", {SEASONS[item['season']]['prompt']} season"
                      if item.get("season") else ""))
        view = _SCENE_HINT.get(usage, _SCENE_HINT["battle"])
    elif kind == "character":
        role = "production character sprite"
        subject = (f"full body single character"
                   + (f", {gender}" if gender else "")
                   + f", standing pose, whole body in frame: {who}")
        view = "centered, " + bg
    elif kind == "pet":
        role = "production game pet companion creature sprite"
        subject = f"full body cute companion creature: {who}"
        view = "centered, " + bg
    elif kind == "weapon":
        tier = WEAPON_TIERS.get(item.get("tier_id"))
        role = "production game weapon design asset"
        subject = (f"single weapon, centered: {who}"
                   + (f", {tier['prompt']} quality tier" if tier else ""))
        view = bg
    elif kind == "building":
        role = "production game architecture concept art"
        subject = f"exterior view, full structure visible: {who}"
        view = "base sitting on an isometric ground plane"
    else:                                   # monster：等级定体型，场景定环境
        tier = MONSTER_TIERS[monster_tier(item)]
        role = "production game monster creature design"
        subject = (f"full body tier-{tier['name']} creature, {tier['prompt']}: {who}"
                   + (f", native to the {_clean(item['scene'])} region"
                      if item.get("scene") else ""))
        view = "centered, " + bg

    ad = _artdir_block(s)
    art_dir = ", ".join(x for x in (_style_block(s), ad) if x)
    extra = _clean(prompt_extra(s, kind))          # 项目级补充（该类独有）
    if extra:
        subject = f"{subject}, {extra}"
    read = ("clean silhouette readable at native game scale, "
            "foot/base anchored consistently" if kind != "scene"
            else "main structure readable at gameplay camera distance")
    view_full = view if kind == "scene" else f"{VIEW_LOCK['prompt']}, {view}"
    technical = f"{view_full}, {anim_tail}" if anim_tail else view_full
    return (f"ROLE/PURPOSE: {role}. "
            f"SUBJECT: {subject}. "
            f"VIEW: {view_full}. "
            f"ART DIRECTION: {art_dir}. "
            f"GAME-SCALE READ: {read}. "
            f"TECHNICAL OUTPUT: {technical}. "
            "LOCKS: Using the reference image strictly as style, color palette and "
            f"lighting anchor (time of day: {get_preset(s['time_preset'])['name']}), "
            "keep style, palette, proportions and atmosphere identical to the reference. "
            f"EXCLUDE: {_COMMON_NEG} cropped edges")


def design_prompt(s: dict, item: dict, dirs: list[str], sheet_idx: int,
                  sheet_total: int) -> str:
    """身份卡提示词（第一层）：一张图并排画同一角色的 N 个朝向。

    写法和单帧提示词不同——这里要的是「版面约束」而不是「姿势约束」：
    逐栏点名朝向、写明等距不重叠、并反复强调 same character / identical，
    因为 2.1 在多主体排布时最容易犯的错就是「三栏画成三个人」。

    刻意不拼 VIEW_LOCK：视图锁里明确禁掉「正面平视」，而身份卡第一栏就要正面。
    身份卡只负责身份与配色，镜头俯仰留给关键姿态那一步（那里有 VIEW_LOCK + 参考图）。
    """
    gender = GENDERS.get(item.get("gender"), {}).get("prompt", "")
    who = f"{_clean(item['name'])} - {_clean(item['desc'])}"
    n = len(dirs)
    cols = " | ".join(
        f"column {i + 1}: {DIRECTIONS[d]['prompt']}"
        for i, d in enumerate(dirs))
    ad = _artdir_block(s)
    art_dir = ", ".join(x for x in (_style_block(s), ad) if x)
    role = ("ROLE/PURPOSE: character turnaround model sheet, the single identity reference "
            "for every animation frame of this character.")
    subject = (f"SUBJECT: full body single character"
               + (f", {gender}" if gender else "")
               + f": {who}. The same ONE character repeated exactly {n} times. "
               + cols + ".")
    layout = (f"VIEW: character turnaround sheet, one row of {n} evenly spaced columns, "
              f"equal spacing, columns never overlap, every column shows the whole body "
              f"from head to feet, plain light neutral background in every column, "
              f"consistent camera height across columns"
              + (f", sheet {sheet_idx + 1} of {sheet_total}" if sheet_total > 1 else "")
              + ".")
    read = ("GAME-SCALE READ: chibi proportions about three heads tall, "
            "clean silhouette readable at game sprite scale, both feet visible.")
    tech = (f"TECHNICAL OUTPUT: {n}-column turnaround sheet, no text, no labels, "
            "no arrows, no column borders, no numbered captions.")
    locks = ("LOCKS: every column is the exact same character - identical face, "
             "identical hairstyle, identical costume pieces and trims, identical color "
             "palette, identical proportions, identical shading; only the camera angle "
             "changes between columns.")
    exclude = (f"EXCLUDE: {_COMMON_NEG} cropped edges, different characters in different "
               "columns, costume changes between columns, color shifts between columns")
    return " ".join([role, subject, layout,
                     f"ART DIRECTION: {art_dir}.", read, tech, locks, exclude])


def asset_negative(s: dict, kind: str) -> str:
    """负面词 = 通用 + 2.5D 视图（场景除外，它有自带取景词）+ 风格 + 时间背景 + 该类补充。"""
    style, preset = get_style(s["style"]), get_preset(s["time_preset"])
    parts = [_COMMON_NEG]
    if kind != "scene":
        parts.append(VIEW_LOCK["negative"])     # 禁掉正面/平视/俯视等破 2.5D 的视角
    parts += [style["negative"], preset["negative"], prompt_negative(s, kind)]
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
        # 透明帧直接当参考图，透明区会被模型当成白色 → 压到中性灰底再喂
        refs.append(_alpha_to_ref(asset["path"]))
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


def _url_of(p: Path) -> str:
    """本地路径 → 前端可访问的 /data/... URL。"""
    try:
        rel = p.resolve().relative_to(settings.data_dir_path.resolve())
        return f"/data/{rel.as_posix()}"
    except ValueError:
        return f"/data/{p.name}"


def recut_asset(s: dict, asset: dict) -> dict:
    """重抠一张素材的透明底（rembg 优先，退化自动回落色键），旧图进版本历史。

    原始底图从当前记录反推：本工具产出的透明图都叫 <原名>_alpha*.png，
    去掉这段后缀就是模型原图；已经是原图则直接用。
    """
    cur = Path(asset["path"])
    base = cur
    if "_alpha" in cur.stem:
        stem = cur.stem.split("_alpha")[0]
        cand = cur.with_name(stem + cur.suffix)
        if cand.is_file():
            base = cand
    if not base.is_file():
        raise RuntimeError(f"找不到原始底图：{base.name}")

    mtime = int(base.stat().st_mtime)
    out = base.with_name(f"{base.stem}_alpha{mtime % 100000}.png")
    method = transparent_mode(s, asset["kind"])
    rep = transmat.matte(base, out, method=method)

    def _apply(t: dict) -> None:
        for i, a in enumerate(t["assets"]):
            if a["id"] != asset["id"]:
                continue
            history = a.get("history") or []
            history.insert(0, {k: a.get(k) for k in
                               ("path", "url", "seed", "tone", "created_at",
                                "prompt", "transparent")})
            del history[_MAX_HISTORY:]
            t["assets"][i] = {**a, "path": str(out), "url": _url_of(out),
                              "transparent": rep, "history": history}
            break
    store.mutate(s["id"], _apply)
    updated = next(a for a in store.get_set(s["id"])["assets"]
                   if a["id"] == asset["id"])
    return updated


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
