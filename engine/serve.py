# -*- coding: utf-8 -*-
"""引擎独立进程的 HTTP 服务（可选）。

默认平台模式下引擎与门户同进程（ENGINE_TRANSPORT=inproc），不必起这个进程。
当你想让多个进程/多台前端共用同一份模型权重时，单独起它：

    python -m uvicorn engine.serve:app --host 127.0.0.1 --port 8189

然后把调用方的 ENGINE_TRANSPORT 设为 http、ENGINE_URL 指向它即可。
（当前 HTTP 传输只覆盖状态与生命周期；出图/文本任务的 HTTP 转发是下一步。）
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from shared.config import settings
from engine.sdk import engine
from engine.infer.manager import manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    await engine.startup()
    try:
        yield
    finally:
        await engine.shutdown()


app = FastAPI(title="Qwen-Image 引擎服务", version="1.0.0", lifespan=lifespan)


@app.get("/engine/v1/status", summary="引擎状态")
async def status():
    return engine.status()


@app.get("/engine/v1/capabilities", summary="引擎能力声明")
async def capabilities():
    return {
        "mode": engine.mode,
        "capabilities": ["txt2img", "img2img", "textgen"],
        "transport": settings.engine_transport,
        "queue_size": engine.queue_size(),
    }


@app.post("/engine/v1/load", summary="加载模型")
async def load():
    await asyncio.get_running_loop().run_in_executor(None, engine.load)
    return {"ok": True, "detail": engine.status()}


@app.post("/engine/v1/unload", summary="释放显存")
async def unload():
    engine.unload()
    return {"ok": True}


@app.post("/engine/v1/recover", summary="复位引擎（hard=重启推理子进程）")
async def recover(hard: bool = True):
    ok, message = engine.recover(hard=hard)
    return {"ok": ok, "message": message}


@app.get("/engine/v1/health", summary="健康检查")
async def health():
    return {
        "ok": True,
        "mode": manager.mode,
        "loaded": manager.is_loaded(),
        "queue_size": engine.queue_size(),
        "last_error": manager.last_error,
    }
