# -*- coding: utf-8 -*-
"""游戏素材流水线 4.0：分类独立生成 + 异常全程透明。

八个子阶段（store.STAGES）各自独立入队、独立进度、独立失败：
    plan(策划) → master(基准图) → scene / character / pet / weapon / building / monster

所有阶段进同一条 FIFO 串行工作队列（保显存顺序），类与类之间互不拖累：
某类失败只标红该类并可单独重试，其他类照常。前端轮询 stages 状态 + 日志。

"all" 是编排入口：按依赖顺序把 plan→master→六类 串起来跑，中途某类失败
继续下一类（可事后单独重试失败类）。

一致性机制不变：风格基因（styles.py）+ 时间背景（presets.py）+ 基准图融合；
季节变体在同一时间背景（色温/光源）下换植被与天气，色调阈值放宽 1.6 倍。
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

from . import store, tonecheck
from .planner import plan_from_idea
from .presets import (get_preset, SEASONS, WEAPON_TIERS, ASSET_KINDS,
                      build_stage_items)
from .styles import get_style

_MASTER_W, _MASTER_H = 1152, 640
_ASSET_W, _ASSET_H = 768, 768
_PLANNING_TIMEOUT = 300        # LLM 策划硬上限（秒），超时取消并降级规则
_SEASON_THRESHOLD_FACTOR = 1.6
_ASSET_KINDS = ("scene", "character", "pet", "weapon", "building", "monster")  # noqa: 保持旧名引用
_PLAN_KEYS = {"scene": "scenes", "character": "characters", "pet": "pets",
              "weapon": "weapons", "building": "buildings", "monster": "monsters"}
_COMMON_NEG = ("text, watermark, signature, ui, frame, border, "
               "low quality, blurry, deformed")


# ============================================================
#  队列：单 worker 串行消费，异常全部落库
# ============================================================

_queue: deque[tuple[str, str]] = deque()
_worker_task: asyncio.Task | None = None
_current: dict[str, str] = {}      # sid -> 正在跑的 kind


def is_busy(sid: str) -> bool:
    """该集是否有阶段在排队/执行（前端 alive 标志 + 删除守门用）。"""
    if _current.get(sid):
        return True
    if any(sid == s and k != "all" for s, k in _queue):
        return True
    s = store.get_set(sid)
    if not s:
        return False
    return any(st["status"] in ("queued", "running")
               for st in s["pipeline"]["stages"].values())


def stage_status(sid: str, kind: str) -> str:
    s = store.get_set(sid)
    return s["pipeline"]["stages"][kind]["status"] if s else "idle"


def enqueue(sid: str, kind: str) -> None:
    """把一个阶段（或 all）加入队列。依赖不满足时抛 ValueError。"""
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

    if kind in store.STAGE_IDS:
        def _mark(t: dict) -> None:
            t["pipeline"]["stages"][kind]["status"] = "queued"
            t["pipeline"]["stages"][kind]["error"] = ""
        store.mutate(sid, _mark)
    store.append_log(sid, "info", f"「{store.STAGE_LABELS.get(kind, '全部生成')}」加入队列")
    _queue.append((sid, kind))
    _ensure_worker()


def _ensure_worker() -> None:
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker())


async def _worker() -> None:
    try:
        while _queue:
            sid, kind = _queue.popleft()
            _current[sid] = kind
            try:
                if kind == "all":
                    await _run_all(sid)
                else:
                    await _run_stage(sid, kind)
            except asyncio.CancelledError:
                # 服务关闭/重启：把状态落库，别让页面永远"进行中"
                _mark_stage(sid, kind, status="failed", error="服务停止，任务中断（可重试）")
                store.append_log(sid, "error", "服务停止，任务中断")
                raise
            except Exception as exc:
                print(f"[gameassets] 阶段 {kind} 异常：{exc}")
                traceback.print_exc()
                _mark_stage(sid, kind, status="failed", error=str(exc)[:200])
                store.append_log(sid, "error",
                                 f"「{store.STAGE_LABELS.get(kind, '全部生成')}」异常：{exc}")
            finally:
                _current.pop(sid, None)
    finally:
        global _worker_task
        _worker_task = None


async def _run_all(sid: str) -> None:
    """编排：plan → master → 六类。已完成（done）的阶段跳过——「全部生成」
    即「续做到全齐」，中断后点生成不会重跑已出的素材；要重做某一类用该类的单独重试。"""
    skipped: list[str] = []
    for kind in ("plan", "master", *ASSET_KINDS):
        if stage_status(sid, kind) == "done":
            skipped.append(store.STAGE_LABELS[kind])
            continue
        try:
            await _run_stage(sid, kind)
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


def _reset_items(sid: str, kind: str) -> list[dict]:
    """把阶段条目重置为 pending，返回条目列表供执行循环用。"""
    items: list[dict] = []

    def _apply(t: dict) -> None:
        st = t["pipeline"]["stages"][kind]
        for it in st["items"]:
            it["status"] = "pending"
            it["error"] = ""
            it["score"] = None
        st["done"] = 0
        st["status"] = "running"
        st["error"] = ""
        items.extend(st["items"])
    store.mutate(sid, _apply)
    return items


def _save_plan_and_items(sid: str, plan: dict) -> None:
    """策划完成：落方案 + 预展开六类条目（前端立刻能看到每类要出几张）。"""
    blueprint = build_stage_items(plan)

    def _apply(t: dict) -> None:
        t["plan"] = plan
        if plan.get("planner") == "llm":
            t["name"] = plan["title"]
        for kind in ASSET_KINDS:
            st = t["pipeline"]["stages"][kind]
            st["items"] = blueprint[kind]
            st["total"] = len(blueprint[kind])
        master_st = t["pipeline"]["stages"]["master"]
        master_st["items"] = [{"name": plan["scenes"][0]["name"],
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
    """同类同名额（含季节）原位替换，重试不产生重复卡。"""
    def _apply(t: dict) -> None:
        t["assets"] = [a for a in t["assets"]
                       if not (a["kind"] == asset["kind"] and a["name"] == asset["name"]
                               and a.get("season") == asset.get("season"))]
        t["assets"].append(asset)
    store.mutate(sid, _apply)


# ============================================================
#  各阶段执行体
# ============================================================

async def _run_stage(sid: str, kind: str) -> None:
    s = store.get_set(sid)
    if not s:
        return
    store.append_log(sid, "info", f"开始「{store.STAGE_LABELS[kind]}」")

    if kind == "plan":
        await _stage_plan(sid, s)
    elif kind == "master":
        await _stage_master(sid)
    else:
        await _stage_assets(sid, kind)

    _mark_stage(sid, kind, status="done")
    store.append_log(sid, "info", f"「{store.STAGE_LABELS[kind]}」完成")


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
    try:
        rec = await _run_job(prompt=prompt, negative=negative,
                             width=_MASTER_W, height=_MASTER_H, seed=-1, ref_path=None)
        palette = [c["hex"] for c in tonecheck.extract_palette(rec["path"])]

        def _save(t: dict, rec=rec, palette=palette) -> None:
            t["master"] = {"path": rec["path"], "url": rec["url"],
                           "seed": rec.get("seed"), "desc": items[0]["desc"],
                           "palette": palette, "prompt": prompt}
        store.mutate(sid, _save)
        _item_update(sid, "master", 0, status="done")
    except Exception as exc:
        _item_update(sid, "master", 0, status="failed", error=str(exc))
        raise


async def _stage_assets(sid: str, kind: str) -> None:
    s = store.get_set(sid)
    if not s.get("master"):
        raise RuntimeError("基准图未就绪，请先生成基准图")
    master_path = s["master"]["path"]
    base_threshold = float(s.get("threshold") or tonecheck.DEFAULT_THRESHOLD)

    # 条目兜底：有方案但条目表空（历史遗留）→ 现场展开
    if not s["pipeline"]["stages"][kind]["items"]:
        def _fill(t: dict) -> None:
            items = build_stage_items(s["plan"])[kind]
            t["pipeline"]["stages"][kind]["items"] = items
            t["pipeline"]["stages"][kind]["total"] = len(items)
        store.mutate(sid, _fill)
        s = store.get_set(sid)

    items = _reset_items(sid, kind)

    n_fail = 0
    for i, item in enumerate(items):
        _item_update(sid, kind, i, status="running")
        try:
            payload_item = {"kind": kind, "name": item["name"], "desc": item["desc"],
                            "season": item.get("season"), "tier_id": item.get("tier_id")}
            prompt = asset_prompt(s, payload_item)
            threshold = base_threshold * (_SEASON_THRESHOLD_FACTOR
                                          if item.get("season") else 1.0)
            rec = await _run_job(prompt=prompt, negative=_COMMON_NEG,
                                 width=_ASSET_W, height=_ASSET_H,
                                 seed=-1, ref_path=master_path)
            tone = tonecheck.check(rec["path"], master_path, threshold=threshold)
            asset = {
                "id": new_id("ast"),
                "kind": kind, "kind_label": store.STAGE_LABELS[kind],
                "name": item["name"], "desc": item["desc"],
                "season": item.get("season"),
                "season_name": SEASONS[item["season"]]["name"] if item.get("season") else None,
                "tier": item.get("tier"), "tier_id": item.get("tier_id"),
                "path": rec["path"], "url": rec["url"], "seed": rec.get("seed"),
                "prompt": prompt, "tone": tone, "created_at": store.now_iso(),
            }
            _upsert_asset(sid, asset)
            _item_update(sid, kind, i, status="done", score=tone["score"])
            verdict = "✓" if tone["pass"] else "✗跑色"
            store.append_log(sid, "info",
                             f"{store.STAGE_LABELS[kind]}「{item['name']}"
                             f"{'·' + SEASONS[item['season']]['name'] if item.get('season') else ''}」"
                             f"完成 色调{tone['score']:.0f} {verdict}")
        except Exception as exc:
            n_fail += 1
            _item_update(sid, kind, i, status="failed", error=str(exc))
            store.append_log(sid, "error",
                             f"{store.STAGE_LABELS[kind]}「{item['name']}」失败：{exc}")
    if n_fail:
        raise RuntimeError(f"{n_fail}/{len(items)} 张失败（可单独重试本类）")


# ============================================================
#  提示词组装（与 3.0 相同：风格基因 + 时间背景不缺勤）
# ============================================================

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
              f"{scene['name']} - {scene['desc']}, {_style_block(s)}, "
              "rich details, no characters, no text")
    return prompt, f"{_COMMON_NEG}, {style['negative']}, {preset['negative']}"


def asset_prompt(s: dict, item: dict) -> str:
    """六类素材 + 四季变体统一组装。item: {kind,name,desc,season?,tier_id?}。"""
    kind = item["kind"]
    anchor = ("Using the reference image strictly as style, color palette and lighting "
              f"anchor (time of day: {get_preset(s['time_preset'])['name']}), ")
    body = {
        "scene":    (f"game environment concept art: {item['name']} - {item['desc']}, "
                     + (f"{SEASONS[item['season']]['prompt']} season, "
                        if item.get("season") else "")
                     + "full detailed environment"),
        "character": (f"full body character design, single character, front view, "
                      f"standing pose, whole body visible: {item['name']} - {item['desc']}, "
                      "plain light neutral background, centered"),
        "pet":      (f"cute game pet companion creature design, full body, side-front view: "
                     f"{item['name']} - {item['desc']}, plain light neutral background, centered"),
        "weapon":   (f"game weapon design, single weapon, centered, three-quarter view: "
                     f"{item['name']} - {item['desc']}, "
                     + (f"{WEAPON_TIERS[item['tier_id']]['prompt']} quality tier, "
                        if item.get("tier_id") else "")
                     + "plain light neutral background"),
        "building": (f"game architecture concept art, exterior view: "
                     f"{item['name']} - {item['desc']}, full structure visible"),
        "monster":  (f"game monster creature design, full body, menacing silhouette: "
                     f"{item['name']} - {item['desc']}, plain light neutral background, centered"),
    }[kind]
    return f"{anchor}{body}. Keep style, palette and atmosphere identical to the reference"


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
#  引擎调用（与 imagegen 同款：先入库再同步生成）
# ============================================================

async def _run_job(*, prompt: str, negative: str, width: int, height: int,
                   seed: int, ref_path: str | None) -> dict:
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
    if ref_path:
        p = Path(ref_path)
        if not p.is_absolute():
            p = Path(".") / p
        data_url = f"data:image/png;base64,{base64.b64encode(p.read_bytes()).decode()}"
        payload.update({
            "mode": "img2img",
            "fusion": True,
            "ref_images": [data_url],
            "negative_prompt": "",
            "guidance_scale": 1.0,
            "strength": 1.0,
        })
    repo.create_job(job_id=payload["job_id"], prompt=prompt, negative=negative,
                    params=payload, seed=seed, count=1,
                    mode="img2img" if ref_path else "txt2img")
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
