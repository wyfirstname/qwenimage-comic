# -*- coding: utf-8 -*-
"""模型与系统状态接口。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from app import repo
from app.config import settings
from app.inference import scheduler
from app.inference.manager import manager
from app.schemas import ConfigOut, ModelStatus, OkOut
from app.services import generation

router = APIRouter(tags=["system"])


@router.get("/system/health", summary="健康检查")
async def health():
    return {"ok": True, "engine_mode": manager.mode, "queue_size": scheduler.queue_size()}


@router.get("/models/status", response_model=ModelStatus, summary="模型状态")
async def model_status():
    st = manager.status()
    st["queue_size"] = scheduler.queue_size()
    st["running_job"] = scheduler.running_job()
    return st


@router.post("/models/load", response_model=OkOut, summary="加载模型")
async def load_model():
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, manager.ensure_loaded)
    except Exception as exc:
        raise HTTPException(status_code=422, detail={"code": "MODEL_NOT_LOADED", "message": str(exc)})
    return {"ok": True, "message": "模型已加载"}


@router.post("/models/unload", response_model=OkOut, summary="卸载模型（释放显存）")
async def unload_model():
    manager.unload()
    return {"ok": True, "message": "模型已卸载"}


@router.get("/system/config", response_model=ConfigOut, summary="生效配置")
async def get_config():
    files = settings.model_files_status()
    return {
        "engine_mode": manager.mode,
        "nsfw_filter": settings.nsfw_filter,
        "default_width": settings.default_width,
        "default_height": settings.default_height,
        "default_steps": settings.default_steps,
        "default_guidance": settings.default_guidance,
        "max_batch": settings.max_batch,
        "max_queue_size": settings.max_queue_size,
        "enable_remote_fallback": settings.enable_remote_fallback,
        "remote_model": settings.remote_model,
        "model_ready": bool(files.get("ready")),
        "model_dir": str(settings.model_dir_path),
        "output_dir": str(settings.output_dir_path),
    }


@router.get("/system/model-files", summary="模型文件清单与下载指引")
async def model_files():
    files = settings.model_files_status()
    return {
        "files": files,
        "model_dir": str(settings.model_dir_path),
        "download_command": (
            "set HF_ENDPOINT=https://hf-mirror.com && "
            "huggingface-cli download abenzerps/Qwen-Image-2.1-Uncensored-GGUF "
            "--include \"qwen-image-2.1-UC-Q4_K_M.gguf\" "
            "--include \"text_encoders/qwen3vl_8b_int8_convrot.safetensors\" "
            "--include \"vae/qwen_image_2.1_vae_bf16.safetensors\" "
            "--local-dir models/qwen-image-2.1-unc"
        ),
    }


@router.post("/system/cleanup", summary="按配额清理旧图")
async def cleanup():
    return generation.cleanup_outputs()


@router.get("/system/stats", summary="运行统计")
async def stats():
    data = repo.gallery_stats()
    data["queue_size"] = scheduler.queue_size()
    data["engine_mode"] = manager.mode
    return data
