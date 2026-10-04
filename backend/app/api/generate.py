# -*- coding: utf-8 -*-
"""文生图 / 图生图接口。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from app import repo
from app.config import settings
from app.inference import scheduler
from app.models_util import new_id
from app.schemas import GenerateRequest, JobOut, PreviewOut, PreviewRequest
from app.services import generation
from app.services.generation import GenerationError

router = APIRouter(tags=["generate"])


def _build_payload(req: GenerateRequest, count: int = 1) -> dict:
    payload = req.model_dump()
    payload["count"] = count
    payload["job_id"] = new_id("job")
    return payload


@router.post("/generate", response_model=JobOut, summary="同步出图（阻塞直到完成）")
async def generate(req: GenerateRequest):
    if settings.engine_mode == "local":
        ok, why = scheduler.vram_guard(1.0)
        # 显存不足不直接拒绝，交由引擎在卸载后重试
    payload = _build_payload(req, count=1)
    repo.create_job(
        job_id=payload["job_id"],
        prompt=req.prompt,
        negative=req.negative_prompt,
        params=payload,
        seed=req.seed,
        count=1,
        mode=req.mode,
    )
    try:
        job = await asyncio.wait_for(
            generation.generate_sync(payload),
            timeout=settings.infer_timeout_seconds,
        )
    except asyncio.TimeoutError:
        repo.mark_job_done(payload["job_id"], "failed", error="推理超时")
        raise HTTPException(status_code=504, detail={"code": "TIMEOUT", "message": "推理超时"})
    except GenerationError as exc:
        raise HTTPException(status_code=500, detail={"code": "INFERENCE_ERROR", "message": str(exc)})

    return _job_with_images(job)


@router.post("/jobs", summary="提交异步任务")
async def create_job(req: GenerateRequest):
    count = max(1, min(req.batch_size, settings.max_batch))
    payload = _build_payload(req, count=count)
    try:
        return await generation.submit_batch(payload)
    except GenerationError as exc:
        raise HTTPException(status_code=409, detail={"code": "BUSY", "message": str(exc)})


@router.get("/jobs", summary="任务列表")
async def list_jobs(limit: int = 20, status: str | None = None):
    return {"items": repo.list_jobs(limit=limit, status=status)}


@router.get("/jobs/{job_id}", response_model=JobOut, summary="任务详情")
async def get_job(job_id: str):
    job = repo.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "任务不存在"})
    return _job_with_images(job)


@router.delete("/jobs/{job_id}", summary="取消 / 删除任务")
async def delete_job(job_id: str):
    job = repo.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "任务不存在"})
    if job["status"] in ("queued", "running"):
        # 必须同时中断引擎侧：只改数据库的话，ComfyUI 会把整段采样跑完，
        # 用户看到「已取消」但 GPU 仍满载十几分钟，后面的任务也被堵住。
        try:
            from app.inference.manager import manager

            engine = manager.engine
            if engine is not None and hasattr(engine, "cancel"):
                engine.cancel()
        except Exception:
            pass
        repo.mark_job_done(job_id, "canceled")
        repo.delete_job(job_id)
    else:
        repo.delete_job(job_id)
    return {"ok": True, "message": "已删除"}


@router.post("/preview", response_model=PreviewOut, summary="参数估算")
async def preview(req: PreviewRequest):
    return generation.estimate(req.model_dump())


def _job_with_images(job: dict) -> dict:
    job = dict(job)
    job["images"] = repo.images_by_job(job["id"])
    return job
