# -*- coding: utf-8 -*-
"""模型与系统状态接口。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from app import comic_repo as crepo
from app import repo
from app.config import settings
from app.inference import scheduler
from app.inference.manager import manager
from app.schemas import ConfigOut, ModelStatus, OkOut
from app.services import generation

router = APIRouter(tags=["system"])


@router.get("/system/health", summary="健康检查")
async def health():
    """轻量健康检查，额外透出「有没有卡住」的信号，供界面提示。"""
    from app.services import comic_ai as cai

    stuck = cai.ai_tasks.stuck_task()
    return {
        "ok": True,
        "engine_mode": manager.mode,
        "queue_size": scheduler.queue_size(),
        "running_job": scheduler.running_job(),
        "ai_running": (cai.ai_tasks.running() or {}).get("id"),
        "ai_stuck": (stuck or {}).get("id"),
        "ai_stalled_seconds": (stuck or {}).get("stalled_seconds", 0.0),
    }


@router.post("/system/recover", summary="卡死恢复：复位引擎并收敛残留任务")
async def recover(hard: bool = True):
    """后台真的卡死时的统一出口。

    做四件事：
      1. 结束卡住的本地 AI 文本任务（剧本 / 角色 / 场景 / 分镜），清空它的队列；
      2. 复位推理引擎——hard=true 会重新拉起 ComfyUI 子进程，卡在等待里的线程随之退出；
      3. 把数据库里「running/queued 但其实没在执行」的出图任务标为中断（可重新发起）；
      4. 收殓漫画分镜 / 场景图 / 角色图的中间态，避免界面永远显示「排队中」。

    全过程在线程池里执行（重启 ComfyUI 要等它重新就绪），不阻塞事件循环。
    """
    loop = asyncio.get_running_loop()

    def _work() -> dict:
        from app.services import comic as comic_service
        from app.services import comic_ai as cai

        report: dict = {"engine": "", "ai": None, "interrupted_jobs": [], "comic_fixed": 0}

        # 1) + 2) AI 任务与引擎（recover 内部会调 manager.recover_engine）
        try:
            report["ai"] = cai.ai_tasks.recover(
                task_id=None, hard=hard, reason="系统一键恢复"
            )
            report["engine"] = report["ai"].get("engine_message", "")
        except Exception as exc:  # pragma: no cover - 防御性
            report["engine"] = f"AI 任务恢复失败：{exc}"

        # 3) 出图任务：调度器没有在跑，却还有 running/queued 的记录 → 已失联
        if scheduler.running_job() is None:
            try:
                stale = repo.list_jobs(limit=200, status="running") + \
                        repo.list_jobs(limit=200, status="queued")
                for job in stale:
                    repo.mark_job_done(job["id"], "failed",
                                       error="已执行「卡死恢复」，任务被中断，可重新发起")
                    report["interrupted_jobs"].append(job["id"])
            except Exception as exc:  # pragma: no cover
                print(f"[恢复] 出图任务收敛跳过：{exc}")

        # 4) 漫画中间态与任务对账（分镜 / 场景图 / 角色图）
        try:
            for project in crepo.list_projects():
                report["comic_fixed"] += comic_service.reconcile_project(project["id"])
        except Exception as exc:  # pragma: no cover
            print(f"[恢复] 漫画状态对账跳过：{exc}")

        return report

    return await loop.run_in_executor(None, _work)


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
