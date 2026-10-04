# -*- coding: utf-8 -*-
"""SSE 进度订阅。"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app import repo
from app.services.events import bus

router = APIRouter(tags=["events"])


@router.get("/jobs/{job_id}/events", summary="任务进度流（SSE）")
async def job_events(job_id: str):
    job = repo.get_job(job_id)
    if not job:
        async def _missing():
            yield 'event: error\ndata: {"error": "任务不存在"}\n\n'
        return StreamingResponse(_missing(), media_type="text/event-stream")

    async def _stream():
        # 先补发一次当前状态，避免订阅前已完成的进度丢失
        yield (
            "event: status\n"
            f'data: {{"status": "{job["status"]}", "progress": {job["progress"]}}}\n\n'
        )
        if job["status"] in ("succeeded", "failed", "canceled"):
            yield f'event: done\ndata: {{"status": "{job["status"]}"}}\n\n'
            return
        async for chunk in bus.stream(job_id):
            yield chunk

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
