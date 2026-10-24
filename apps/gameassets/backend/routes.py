# -*- coding: utf-8 -*-
"""游戏素材工坊接口。

一致性三层机制（缺一不可，这是本模块的存在理由）：
  1. 提示词层：时间背景预设的色调/光照片段 + 统一负面词，进每一张图；
  2. 图像层：先出「场景基准图」，之后所有素材走 fusion 编辑通道挂基准图
     （同 storyboard 定妆照机制：mode=img2img + ref_images + CFG 1.0）；
  3. 后验层：每张素材出图后做主色提取，与基准色板算距离，
     tone.pass=False 的素材前端标黄，提示重出——「色调一致」是算出来的，不是喊出来的。
"""

from __future__ import annotations

import asyncio
import base64

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from shared import repo
from shared.config import settings
from shared.models_util import new_id
from engine.sdk import engine, GenerationError

from . import store, tonecheck
from .presets import TIME_PRESETS, get_preset

router = APIRouter(prefix="/gameassets", tags=["gameassets"])

_MASTER_W, _MASTER_H = 1152, 640   # 横版基准图，16:9 游戏场景
_ASSET_W, _ASSET_H = 768, 768      # 单件素材方形底
_KINDS = {"character": "角色", "scene": "场景", "prop": "道具"}

_COMMON_NEG = ("text, watermark, signature, ui, frame, border, "
               "low quality, blurry, deformed")


class SetIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    time_preset: str = Field(..., description="时间背景 id，见 /presets")
    desc: str = Field("", max_length=500, description="美术风格补充描述")
    threshold: float = Field(tonecheck.DEFAULT_THRESHOLD, ge=10, le=120)


class MasterIn(BaseModel):
    desc: str = Field(..., min_length=1, max_length=300, description="场景内容描述")


class AssetIn(BaseModel):
    kind: str = Field(..., description="character | scene | prop")
    desc: str = Field(..., min_length=1, max_length=300)


@router.get("/presets", summary="时间背景预设")
async def list_presets():
    return {"items": list(TIME_PRESETS.values())}


@router.get("/sets", summary="素材集列表")
async def list_sets():
    return {"items": store.list_sets()}


@router.post("/sets", summary="创建素材集")
async def create_set(req: SetIn):
    try:
        preset = get_preset(req.time_preset)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail={"code": "BAD_PRESET", "message": str(exc)})
    s = store.make_set(name=req.name.strip(), time_preset=preset["id"],
                       time_name=preset["name"], desc=req.desc.strip(),
                       threshold=req.threshold)
    return store.add_set(s)


@router.get("/sets/{sid}", summary="素材集详情")
async def get_set(sid: str):
    s = store.get_set(sid)
    if not s:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "素材集不存在"})
    return s


@router.delete("/sets/{sid}", summary="删除素材集")
async def delete_set(sid: str):
    if not store.delete_set(sid):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "素材集不存在"})
    return {"ok": True}


@router.post("/sets/{sid}/master", summary="生成场景基准图（全素材集的色调锚点）")
async def gen_master(sid: str, req: MasterIn):
    s = store.get_set(sid)
    if not s:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "素材集不存在"})
    preset = get_preset(s["time_preset"])
    style = str(s.get("desc") or "").strip()

    prompt = (f"game concept art, environment establishing shot, {req.desc.strip()}, "
              f"{style + ', ' if style else ''}{preset['prompt']}, "
              "rich details, no characters, no text")
    negative = f"{_COMMON_NEG}, {preset['negative']}"

    job_id = await _run_job(prompt=prompt, negative=negative,
                            width=_MASTER_W, height=_MASTER_H, seed=-1, ref=None)
    img = _first_image(job_id)
    palette = [c["hex"] for c in tonecheck.extract_palette(img["path"])]

    def _apply(t: dict) -> None:
        t["master"] = {
            "path": img["path"], "url": img["url"], "seed": img["seed"],
            "desc": req.desc.strip(), "palette": palette,
            "prompt": prompt,
        }
    store.mutate(sid, _apply)
    return store.get_set(sid)


@router.post("/sets/{sid}/assets", summary="生成素材（自动挂基准图融合 + 色调校验）")
async def gen_asset(sid: str, req: AssetIn):
    s = store.get_set(sid)
    if not s:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "素材集不存在"})
    master = s.get("master")
    if not master:
        raise HTTPException(status_code=409, detail={
            "code": "NEED_MASTER",
            "message": "先出场景基准图：它是全素材集的色调与光照锚点",
        })
    kind_label = _KINDS.get(req.kind)
    if not kind_label:
        raise HTTPException(status_code=400, detail={
            "code": "BAD_KIND", "message": f"kind 只支持：{', '.join(_KINDS)}"})
    preset = get_preset(s["time_preset"])

    prompt = (
        f"Using the reference image only as color palette and lighting anchor "
        f"(time of day: {preset['name']}), generate a single game asset: "
        f"{kind_label} - {req.desc.strip()}. "
        "Keep exactly the same color palette, color temperature, light direction "
        "and atmosphere as the reference. Change subject content only. "
        "Clean composition, centered subject, no text"
    )

    ref_url = _to_data_url(master["path"])
    job_id = await _run_job(prompt=prompt, negative=_COMMON_NEG,
                            width=_ASSET_W, height=_ASSET_H,
                            seed=-1, ref=ref_url)
    img = _first_image(job_id)
    tone = tonecheck.check(img["path"], master["path"],
                           threshold=float(s.get("threshold") or tonecheck.DEFAULT_THRESHOLD))

    asset = {
        "id": new_id("ast"),
        "kind": req.kind,
        "kind_label": kind_label,
        "desc": req.desc.strip(),
        "path": img["path"], "url": img["url"], "seed": img["seed"],
        "prompt": prompt,
        "tone": tone,
        "created_at": store.now_iso(),
    }

    def _apply(t: dict) -> None:
        t["assets"].insert(0, asset)
    store.mutate(sid, _apply)
    return asset


@router.delete("/sets/{sid}/assets/{aid}", summary="删除素材")
async def delete_asset(sid: str, aid: str):
    removed = {"ok": False}

    def _apply(t: dict) -> None:
        before = len(t["assets"])
        t["assets"] = [a for a in t["assets"] if a["id"] != aid]
        removed["ok"] = len(t["assets"]) < before
    store.mutate(sid, _apply)
    if not removed["ok"]:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "素材不存在"})
    return {"ok": True}


# ============================================================
#  引擎调用（与 imagegen 同款：先入库再同步生成，超时 504）
# ============================================================

async def _run_job(*, prompt: str, negative: str, width: int, height: int,
                   seed: int, ref: str | None) -> dict:
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
        "gameassets": {"kind": "asset"},
    }
    if ref:
        payload.update({
            "mode": "img2img",
            "fusion": True,
            "ref_images": [ref],
            "negative_prompt": "",      # fusion 通道不吃负面词（同 storyboard）
            "guidance_scale": 1.0,
            "strength": 1.0,
        })
    repo.create_job(job_id=payload["job_id"], prompt=prompt, negative=negative,
                    params=payload, seed=seed, count=1,
                    mode="img2img" if ref else "txt2img")
    if settings.engine_mode == "local":
        engine.vram_guard(1.0)
    try:
        job = await asyncio.wait_for(
            engine.generate_sync(payload),
            timeout=settings.infer_timeout_seconds,
        )
    except asyncio.TimeoutError:
        repo.mark_job_done(payload["job_id"], "failed", error="推理超时")
        raise HTTPException(status_code=504, detail={"code": "TIMEOUT", "message": "推理超时"})
    except GenerationError as exc:
        raise HTTPException(status_code=500, detail={"code": "INFERENCE_ERROR", "message": str(exc)})
    if job.get("status") != "succeeded":
        raise HTTPException(status_code=500, detail={
            "code": "INFERENCE_ERROR", "message": job.get("error") or "生成失败"})
    return payload["job_id"]


def _first_image(job_id: str) -> dict:
    """generate_sync 的返回体不带图片（入库为准），按 imagegen 同款从 repo 取。"""
    imgs = repo.images_by_job(job_id)
    if not imgs:
        raise HTTPException(status_code=500, detail={
            "code": "INFERENCE_ERROR", "message": "引擎没有返回图片"})
    rec = dict(imgs[0])
    path = rec.get("path") or ""
    rec["url"] = _data_url_to_http(path) if path else (rec.get("url") or "")
    return rec


def _to_data_url(path: str) -> str:
    from pathlib import Path as _P

    p = _P(path)
    if not p.is_absolute():
        p = _P(".") / p
    if not p.exists():
        raise HTTPException(status_code=500, detail={
            "code": "MASTER_MISSING", "message": f"基准图文件丢失：{p}"})
    return f"data:image/png;base64,{base64.b64encode(p.read_bytes()).decode()}"


def _data_url_to_http(path: str) -> str:
    """引擎落盘路径 → 可访问的 /data 静态 URL。"""
    from pathlib import Path as _P

    p = _P(path).resolve()
    try:
        rel = p.relative_to(settings.data_dir_path.resolve())
        return f"/data/{rel.as_posix()}"
    except ValueError:
        return f"/data/{p.name}"


# ============================================================
#  独立运行入口
# ============================================================
def create_app():
    """uvicorn apps.gameassets.backend.routes:create_app --factory --port 8107"""
    from pathlib import Path as _Path

    from fastapi import FastAPI

    from shared.webapp import mount_frontend, mount_media

    application = FastAPI(title="游戏素材工坊", version="1.0.0")
    application.include_router(router, prefix="/api/v1")

    mount_media(application)
    frontend = _Path(__file__).resolve().parents[1] / "frontend"
    mount_frontend(application, frontend, at="/", name="index")
    return application
