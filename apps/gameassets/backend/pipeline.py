# -*- coding: utf-8 -*-
"""一键流水线 3.0：一句话 → 自动策划 → 整包产出。

阶段链（后台任务，逐步写库可轮询）：
    策划中 → 基准图 → 场景×四季 → 角色 → 宠物 → 武器（按等级）→ 建筑 → 野怪

风格一致性 = 风格基因（styles.py）+ 时间背景（presets.py）+ 基准图融合。
季节变体在同一时间背景（色温/光源）下换植被与天气，色调阈值放宽 1.6 倍
（季节合法偏色，不是跑色）。策划失败/超时自动降级规则模板，链子不断。
"""

from __future__ import annotations

import asyncio
import base64
import threading
import traceback
from pathlib import Path

from shared import repo
from shared.config import settings
from shared.models_util import new_id
from engine.sdk import engine, GenerationError

from . import store, tonecheck
from .planner import plan_from_idea
from .presets import get_preset, SEASONS, WEAPON_TIERS
from .styles import get_style

_MASTER_W, _MASTER_H = 1152, 640
_ASSET_W, _ASSET_H = 768, 768
_PLANNING_TIMEOUT = 300        # LLM 策划硬上限（秒），超时取消并降级规则
_SEASON_THRESHOLD_FACTOR = 1.6

_KIND_LABELS = {"scene": "场景", "character": "角色", "pet": "宠物",
                "weapon": "武器", "building": "建筑", "monster": "野怪"}
_MAX_STEPS = 40

_COMMON_NEG = ("text, watermark, signature, ui, frame, border, "
               "low quality, blurry, deformed")

_running: dict[str, asyncio.Task] = {}


def is_running(sid: str) -> bool:
    task = _running.get(sid)
    return bool(task and not task.done())


def start(sid: str) -> None:
    if is_running(sid):
        raise RuntimeError("该素材集的流水线正在运行")
    _running[sid] = asyncio.create_task(_run(sid))


# ============================================================
#  提示词组装（所有素材统一出口：风格基因 + 时间背景不缺勤）
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
        except Exception as exc:            # plan_from_ide意 不该抛，双保险
            result["error"] = str(exc)

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout=budget)
    if th.is_alive():
        # 超时：取消 ComfyUI 侧排队的文本任务，别让它堵住后面的出图
        try:
            engine.text_cancel()
        except Exception:
            pass
        th.join(timeout=10)
        raise TimeoutError(f"LLM 策划超过 {int(budget)} 秒，降级规则模板")
    if "error" in result:
        raise RuntimeError(result["error"])
    return result["plan"]


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


# ============================================================
#  步骤表：由方案展开（场景第 1 条作基准，其余 ×4 季）
# ============================================================

def build_steps(plan: dict) -> list[dict]:
    steps: list[dict] = []
    scenes = plan["scenes"]
    steps.append({"key": "master", "kind": "master", "kind_label": "基准",
                  "name": scenes[0]["name"], "desc": scenes[0]["desc"],
                  "status": "pending", "asset_id": None, "score": None, "error": ""})
    plan_keys = {"scene": "scenes", "character": "characters", "pet": "pets",
                 "weapon": "weapons", "building": "buildings", "monster": "monsters"}
    for kind in ("scene", "character", "pet", "weapon", "building", "monster"):
        for i, item in enumerate(plan[plan_keys[kind]]):
            if kind == "scene":
                # 第 1 条场景已用作基准图，但它的四季变体照出
                for season in ("spring", "summer", "autumn", "winter"):
                    steps.append({
                        "key": new_id("step"), "kind": "scene", "kind_label": "场景",
                        "name": item["name"], "desc": item["desc"], "season": season,
                        "status": "pending", "asset_id": None, "score": None, "error": "",
                    })
                continue
            steps.append({
                "key": new_id("step"), "kind": kind, "kind_label": _KIND_LABELS[kind],
                "name": item["name"], "desc": item["desc"],
                "tier_id": item.get("tier_id"), "tier": item.get("tier"),
                "status": "pending", "asset_id": None, "score": None, "error": "",
            })
    return steps[:_MAX_STEPS]


# ============================================================
#  流水线主体
# ============================================================

async def _run(sid: str) -> None:
    try:
        s = store.get_set(sid)
        if not s:
            return

        # ---- 阶段 1：策划（LLM 或规则，均可降级）----
        def _planning(t: dict) -> None:
            t["pipeline"] = {"status": "planning", "steps": [], "total": 0, "done": 0,
                             "started_at": store.now_iso(), "finished_at": None, "error": ""}
        store.mutate(sid, _planning)
        try:
            plan = await asyncio.to_thread(
                _plan_with_budget, s["idea"], s["scale"], _PLANNING_TIMEOUT)
        except Exception as exc:
            print(f"[gameassets] 策划降级规则模板：{exc}")
            plan = await asyncio.to_thread(_rule_direct, s["idea"], s["scale"])

        def _save_plan(t: dict) -> None:
            t["plan"] = plan
            t["name"] = plan["title"] if plan.get("planner") == "llm" else t["name"]
            steps = build_steps(plan)
            t["pipeline"]["steps"] = steps
            t["pipeline"]["total"] = len(steps)
            t["pipeline"]["status"] = "running"
        store.mutate(sid, _save_plan)

        # ---- 阶段 2：出图 ----
        s = store.get_set(sid)
        style = get_style(s["style"])
        preset = get_preset(s["time_preset"])
        base_threshold = float(s.get("threshold") or tonecheck.DEFAULT_THRESHOLD)
        failed = 0
        master_path: str | None = None

        for i, step in enumerate(s["pipeline"]["steps"]):
            def _running(t: dict, i=i) -> None:
                t["pipeline"]["steps"][i]["status"] = "running"
            store.mutate(sid, _running)
            try:
                if step["kind"] == "master":
                    prompt, negative = master_prompt(s)
                    rec = await _run_job(prompt=prompt, negative=negative,
                                         width=_MASTER_W, height=_MASTER_H,
                                         seed=-1, ref_path=None)
                    palette = [c["hex"] for c in tonecheck.extract_palette(rec["path"])]
                    master_path = rec["path"]

                    def _save_master(t: dict, rec=rec, palette=palette) -> None:
                        t["master"] = {"path": rec["path"], "url": rec["url"],
                                       "seed": rec.get("seed"), "desc": step["desc"],
                                       "palette": palette, "prompt": prompt}
                    store.mutate(sid, _save_master)
                    asset = None
                else:
                    if not master_path:
                        raise RuntimeError("基准图未就绪")
                    item = {"kind": step["kind"], "name": step["name"],
                            "desc": step["desc"], "season": step.get("season"),
                            "tier_id": step.get("tier_id")}
                    prompt = asset_prompt(s, item)
                    threshold = base_threshold * (_SEASON_THRESHOLD_FACTOR
                                                  if step.get("season") else 1.0)
                    rec = await _run_job(prompt=prompt, negative=_COMMON_NEG,
                                         width=_ASSET_W, height=_ASSET_H,
                                         seed=-1, ref_path=master_path)
                    tone = tonecheck.check(rec["path"], master_path, threshold=threshold)
                    asset = {
                        "id": new_id("ast"),
                        "kind": step["kind"], "kind_label": step["kind_label"],
                        "name": step["name"], "desc": step["desc"],
                        "season": step.get("season"),
                        "season_name": SEASONS[step["season"]]["name"] if step.get("season") else None,
                        "tier": step.get("tier"), "tier_id": step.get("tier_id"),
                        "path": rec["path"], "url": rec["url"], "seed": rec.get("seed"),
                        "prompt": prompt, "tone": tone,
                        "created_at": store.now_iso(),
                    }

                    def _save_asset(t: dict, a=asset) -> None:
                        t["assets"].append(a)
                    store.mutate(sid, _save_asset)

                def _step_done(t: dict, i=i, asset=asset) -> None:
                    st = t["pipeline"]["steps"][i]
                    st["status"] = "done"
                    st["asset_id"] = asset["id"] if asset else None
                    if asset:
                        st["score"] = asset["tone"]["score"]
                        st["pass"] = asset["tone"]["pass"]
                    t["pipeline"]["done"] += 1
                store.mutate(sid, _step_done)
            except Exception as exc:    # 单步失败不断链
                failed += 1
                print(f"[gameassets] 步骤失败 {step['kind']}:{step.get('name')}：{exc}")
                traceback.print_exc()

                def _step_fail(t: dict, i=i, msg=str(exc)) -> None:
                    st = t["pipeline"]["steps"][i]
                    st["status"] = "failed"
                    st["error"] = msg[:200]
                    t["pipeline"]["done"] += 1
                store.mutate(sid, _step_fail)

        def _finish(t: dict) -> None:
            p = t["pipeline"]
            p["status"] = "done_with_errors" if failed else "done"
            p["finished_at"] = store.now_iso()
            if failed:
                p["error"] = f"{failed} 步失败，其余已完成"
        store.mutate(sid, _finish)
    except Exception as exc:            # 兜底：整体异常也要落状态
        print(f"[gameassets] 流水线异常：{exc}")
        traceback.print_exc()

        def _fail_all(t: dict) -> None:
            t["pipeline"]["status"] = "failed"
            t["pipeline"]["error"] = str(exc)[:200]
        store.mutate(sid, _fail_all)
    finally:
        _running.pop(sid, None)


def _rule_direct(idea: str, scale_id: str) -> dict:
    """绕过 LLM 直接走规则模板（策划超时后的快速路径，永不超时）。"""
    from .planner import SCALES, _rule_plan, _normalize
    return _normalize(_rule_plan(idea), SCALES.get(scale_id, SCALES["standard"]))
