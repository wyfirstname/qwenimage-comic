# -*- coding: utf-8 -*-
"""人像编辑接口（Qwen-Image 2.1 图像编辑）。

出图复用既有出图队列（`generation.submit_batch`），
任务参数走 `mode=img2img` + `fusion=True` → 引擎的 `TextEncodeQwenImage21`
多参考图编辑通道，与主界面的传统图生图、漫画的分镜融合互不影响。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app import repo
from app.models_util import new_id
from app.schemas import OkOut, PortraitComposeIn, PortraitSubmitIn
from app.services import generation, portrait
from app.services.generation import GenerationError
from app.services.portrait import PortraitError

router = APIRouter(tags=["portrait"], prefix="/portrait")


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400,
                         detail={"code": "BAD_REQUEST", "message": str(exc)})


def _job_with_images(job: dict) -> dict:
    job = dict(job)
    job["images"] = repo.images_by_job(job["id"])
    return job


# ============================================================
#  预设与提示词组装
# ============================================================

@router.get("/presets", summary="人像玩法预设（官方玩法清单）")
async def presets():
    return portrait.presets_payload()


@router.get("/llm/status", summary="本地文本模型状态（智能改写）")
async def llm_status():
    return portrait.llm_status()


@router.post("/compose", summary="组装提示词（只预览，不入队）")
async def compose(req: PortraitComposeIn):
    try:
        # 预览宽容：还没上传参考图也应能组装，数量不足只给提示
        return portrait.build_prompt(
            req.preset, req.values, req.image_count, req.prompt_override, strict=False
        )
    except PortraitError as exc:
        raise _bad_request(exc)


# ============================================================
#  出图（复用出图队列）
# ============================================================

@router.post("/submit", summary="提交人像编辑出图任务")
async def submit(req: PortraitSubmitIn):
    try:
        built = portrait.build_generation_payload(
            preset_id=req.preset,
            values=req.values,
            images_data=req.images,
            prompt_override=req.prompt_override,
            steps=req.steps,
            seed=req.seed,
            count=req.count,
            width=req.width,
            height=req.height,
            max_mp=req.max_mp,
        )
    except PortraitError as exc:
        raise _bad_request(exc)

    payload = built["payload"]
    payload["job_id"] = new_id("job")
    try:
        created = await generation.submit_batch(payload)
    except GenerationError as exc:
        raise HTTPException(status_code=409,
                            detail={"code": "BUSY", "message": str(exc)})

    return {
        **created,
        "preset": payload["portrait"]["preset"],
        "name": payload["portrait"]["name"],
        "level": payload["portrait"]["level"],
        "prompt": payload["prompt"],
        "sections": built["info"]["sections"],
        "width": payload["width"],
        "height": payload["height"],
        "steps": payload["steps"],
        "warnings": built["warnings"],
    }


@router.get("/jobs/{job_id}", summary="人像任务详情（含出图）")
async def job_detail(job_id: str):
    job = repo.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404,
                            detail={"code": "NOT_FOUND", "message": "任务不存在"})
    return _job_with_images(job)


# ============================================================
#  智能改写（官方建议：图像编辑先做提示词改写，结果才稳定）
# ============================================================

@router.post("/rewrite", summary="AI 改写提示词（后台任务）")
async def rewrite(req: PortraitComposeIn):
    try:
        info = portrait.build_prompt(req.preset, req.values, req.image_count, "")
        task = portrait.rewrite_tasks.submit(
            preset_id=req.preset,
            values=req.values,
            image_count=req.image_count,
            base_prompt=info["auto_prompt"],
        )
    except PortraitError as exc:
        raise _bad_request(exc)
    return task


@router.get("/rewrite/tasks", summary="改写任务列表")
async def rewrite_tasks(limit: int = 10):
    return {"items": portrait.rewrite_tasks.list_recent(limit=limit)}


@router.get("/rewrite/tasks/{task_id}", summary="改写任务状态")
async def rewrite_task(task_id: str):
    task = portrait.rewrite_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404,
                            detail={"code": "NOT_FOUND", "message": "任务不存在"})
    return task


@router.post("/rewrite/tasks/{task_id}/cancel", summary="取消改写任务",
             response_model=OkOut)
async def rewrite_cancel(task_id: str):
    ok = portrait.rewrite_tasks.cancel(task_id)
    return OkOut(ok=ok, message="已取消" if ok else "任务已结束")
