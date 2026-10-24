# -*- coding: utf-8 -*-
"""一键流水线：一次配置，整包产出。

步骤链（串行执行——单卡显存就一个坑，排队比并发稳）：
    1. 场景基准图（风格 + 时间背景 + 场景清单第 1 条；它是全画风的锚）
    2. 其余场景 ×N   —— fusion 挂基准图
    3. 角色 ×N       —— fusion 挂基准图（全身立绘、干净底色）
    4. 道具 ×N       —— fusion 挂基准图
每步完成后立即写库（进度可轮询），单步失败不炸整链，最后汇总 done_with_errors。

风格一致性 = 风格基因（styles.py）+ 时间背景（presets.py）+ 基准图融合
三层叠加，流水线里没有任何一步绕开它们。
"""

from __future__ import annotations

import asyncio
import base64
import traceback
from pathlib import Path

from shared import repo
from shared.config import settings
from shared.models_util import new_id
from engine.sdk import engine, GenerationError

from . import store, tonecheck
from .presets import get_preset
from .styles import get_style

_MASTER_W, _MASTER_H = 1152, 640
_ASSET_W, _ASSET_H = 768, 768

_COMMON_NEG = ("text, watermark, signature, ui, frame, border, "
               "low quality, blurry, deformed")

_running: dict[str, asyncio.Task] = {}


def is_running(sid: str) -> bool:
    task = _running.get(sid)
    return bool(task and not task.done())


def start(sid: str) -> None:
    """启动流水线（同一素材集同时只允许一条）。"""
    if is_running(sid):
        raise RuntimeError("该素材集的流水线正在运行")
    _running[sid] = asyncio.create_task(_run(sid))


# ============================================================
#  提示词组装（全部经此出口，保证风格基因/时间背景不缺勤）
# ============================================================

def _master_prompt(style: dict, preset: dict, s: dict, scene_desc: str) -> tuple[str, str]:
    user_style = str(s.get("desc") or "").strip()
    prompt = (f"game concept art, environment establishing shot, {scene_desc}, "
              f"{user_style + ', ' if user_style else ''}{style['prompt']}, {preset['prompt']}, "
              "rich details, no characters, no text")
    return prompt, f"{_COMMON_NEG}, {style['negative']}, {preset['negative']}"


def _asset_prompt(style: dict, preset: dict, s: dict, kind_label: str,
                  desc: str, *, clean_bg: bool) -> str:
    user_style = str(s.get("desc") or "").strip()
    bg = ("plain soft gradient background matching the reference palette, centered subject"
          if clean_bg else "full detailed environment")
    return (f"Using the reference image strictly as style, color palette and lighting anchor "
            f"(time of day: {preset['name']}), generate one {kind_label} game asset: {desc}. "
            f"{user_style + ', ' if user_style else ''}{style['prompt']}, {preset['prompt']}, "
            f"{bg}. Keep style, palette and atmosphere identical to the reference")


def _character_prompt(style: dict, preset: dict, s: dict, desc: str) -> str:
    """角色是全身立绘，底要干净（游戏素材要抠图用）。"""
    user_style = str(s.get("desc") or "").strip()
    return (
        f"Using the reference image strictly as style, color palette and lighting anchor "
        f"(time of day: {preset['name']}), full body character design, single character, "
        f"front view, standing pose, whole body visible: {desc}. "
        f"{user_style + ', ' if user_style else ''}{style['prompt']}, {preset['prompt']}, "
        "plain light neutral background, centered, no text"
    )


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
            "negative_prompt": "",      # fusion 通道不吃负面词（同 storyboard）
            "guidance_scale": 1.0,
            "strength": 1.0,
            "width": width,             # fusion 实际尺寸随 image_1，这里仅声明意图
            "height": height,
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
    rec["url"] = _to_http_url(rec.get("path") or "")
    return rec


def _to_http_url(path: str) -> str:
    p = Path(path).resolve()
    try:
        rel = p.relative_to(settings.data_dir_path.resolve())
        return f"/data/{rel.as_posix()}"
    except ValueError:
        return f"/data/{p.name}"


# ============================================================
#  流水线主体
# ============================================================

def _build_steps(s: dict) -> list[dict]:
    briefs = s.get("briefs") or {}
    steps = [{"key": "master", "kind": "master", "kind_label": "基准",
              "desc": (briefs.get("scenes") or ["主场景"])[0],
              "status": "pending", "asset_id": None, "score": None, "error": ""}]
    labels = {"scene": "场景", "character": "角色", "prop": "道具"}
    scene_rest = (briefs.get("scenes") or [])[1:]     # 第 1 条场景已用作基准图
    for kind, descs in (("scene", scene_rest),
                        ("character", briefs.get("characters") or []),
                        ("prop", briefs.get("props") or [])):
        for desc in descs:
            steps.append({"key": new_id("step"), "kind": kind,
                          "kind_label": labels[kind],
                          "desc": desc, "status": "pending",
                          "asset_id": None, "score": None, "error": ""})
    return steps


async def _run(sid: str) -> None:
    try:
        s = store.get_set(sid)
        if not s:
            return
        style = get_style(s["style"])
        preset = get_preset(s["time_preset"])
        steps = _build_steps(s)
        threshold = float(s.get("threshold") or tonecheck.DEFAULT_THRESHOLD)

        def _init(t: dict) -> None:
            t["pipeline"] = {"status": "running", "steps": steps,
                             "total": len(steps), "done": 0,
                             "started_at": store.now_iso(), "finished_at": None,
                             "error": ""}
        store.mutate(sid, _init)

        failed = 0
        master_path: str | None = None

        for i, step in enumerate(steps):
            def _running(t: dict, i=i) -> None:
                t["pipeline"]["steps"][i]["status"] = "running"
            store.mutate(sid, _running)
            try:
                if step["kind"] == "master":
                    prompt, negative = _master_prompt(style, preset, s, step["desc"])
                    rec = await _run_job(prompt=prompt, negative=negative,
                                         width=_MASTER_W, height=_MASTER_H,
                                         seed=-1, ref_path=None)
                    palette = [c["hex"] for c in tonecheck.extract_palette(rec["path"])]
                    master_path = rec["path"]

                    def _save_master(t: dict, rec=rec, palette=palette) -> None:
                        t["master"] = {
                            "path": rec["path"], "url": rec["url"],
                            "seed": rec.get("seed"), "desc": step["desc"],
                            "palette": palette, "prompt": prompt,
                        }
                    store.mutate(sid, _save_master)
                    asset = None
                else:
                    if not master_path:
                        raise RuntimeError("基准图未就绪")
                    if step["kind"] == "character":
                        prompt = _character_prompt(style, preset, s, step["desc"])
                    else:
                        prompt = _asset_prompt(style, preset, s,
                                               step["kind_label"], step["desc"],
                                               clean_bg=(step["kind"] == "prop"))
                    rec = await _run_job(prompt=prompt, negative=_COMMON_NEG,
                                         width=_ASSET_W, height=_ASSET_H,
                                         seed=-1, ref_path=master_path)
                    tone = tonecheck.check(rec["path"], master_path, threshold=threshold)
                    asset = {
                        "id": new_id("ast"),
                        "kind": step["kind"], "kind_label": step["kind_label"],
                        "desc": step["desc"],
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
                    t["pipeline"]["done"] += 1
                store.mutate(sid, _step_done)
            except Exception as exc:  # 单步失败不断链
                failed += 1
                print(f"[gameassets] 步骤失败 {step['kind']}:{step['desc']}：{exc}")
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
    except Exception as exc:  # 兜底：流水线整体异常也要落状态
        print(f"[gameassets] 流水线异常：{exc}")
        traceback.print_exc()

        def _fail_all(t: dict) -> None:
            t["pipeline"]["status"] = "failed"
            t["pipeline"]["error"] = str(exc)[:200]
        store.mutate(sid, _fail_all)
    finally:
        _running.pop(sid, None)
