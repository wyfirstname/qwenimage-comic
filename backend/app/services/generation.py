# -*- coding: utf-8 -*-
"""出图业务：把调度器、引擎、进度事件与落盘串起来。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional

from PIL import Image

from app import blobs, repo
from app.config import settings
from app.inference import device as dev
from app.inference.manager import manager
from app.inference.scheduler import vram_guard
from app.models_util import (
    decode_base64_image,
    new_id,
    now_iso,
    relative_url,
    save_image,
    stored_path,
)
from app.services import content_policy
from app.services.events import bus

class GenerationError(RuntimeError):
    """出图失败，message 直接面向用户。"""


def _prepare_references(payload: Optional[dict]) -> list[Image.Image]:
    """解析参考图列表。

    * fusion（分镜融合）：payload["ref_images"] 是有序列表，
      第 1 张 = 编辑画布（image_1，决定输出尺寸），其后为参考素材（角色立绘等）；
    * 传统图生图：只用单张。
    """
    payload = payload or {}
    mode = payload.get("mode", "txt2img")
    if mode != "img2img":
        return []

    data_list: list[str] = []
    if payload.get("fusion"):
        data_list = [d for d in (payload.get("ref_images") or []) if d]
        if not data_list and payload.get("image"):
            data_list = [payload["image"]]
    else:
        data = payload.get("image")
        if not data:
            refs = payload.get("ref_images") or []
            data = refs[0] if refs else None
        if data:
            data_list = [data]

    if not data_list:
        raise GenerationError("图生图模式需要提供参考图")

    out: list[Image.Image] = []
    try:
        for data in data_list:
            out.append(decode_base64_image(data))
    except Exception as exc:
        for img in out:
            img.close()
        raise GenerationError(f"参考图解析失败：{exc}") from exc
    return out


def _run_once(payload: dict, job_id: str, index: int, progress_cb) -> dict:
    """执行单张出图（在子线程中运行）。"""
    references = _prepare_references(payload)
    seed = payload.get("seed", -1)
    if seed is not None and seed >= 0:
        seed = seed + index  # 批量时保证每张不同

    fusion = bool(references) and bool(payload.get("fusion"))
    try:
        result = manager.engine.generate(  # type: ignore[union-attr]
            prompt=payload["prompt"],
            negative=payload.get("negative_prompt", ""),
            width=payload["width"],
            height=payload["height"],
            steps=payload["steps"],
            guidance_scale=payload["guidance_scale"],
            seed=seed,
            # 融合模式走 Qwen-Image 2.1 原生多参考图编辑；否则退回传统单图图生图
            references=references if fusion else None,
            reference=None if fusion else (references[0] if references else None),
            strength=payload.get("strength", 0.6),
            progress_cb=progress_cb,
        )
    finally:
        for img in references:
            img.close()

    image: Image.Image = result.image
    # 漫画工作室的出图带 output_subdir（按项目归档），主界面普通出图退回按日期分目录
    path: Path = save_image(image, payload["prompt"], result.seed,
                            subdir=payload.get("output_subdir"))

    flagged, reason = content_policy.evaluate(
        payload["prompt"], payload.get("negative_prompt", "")
    )

    record = {
        "id": new_id("img"),
        "job_id": job_id,
        # 存相对路径（data/...），整目录拷贝到别的电脑 / 盘符后依然有效
        "path": stored_path(path),
        "url": relative_url(path),
        "width": image.width,
        "height": image.height,
        "steps": payload["steps"],
        "guidance": payload["guidance_scale"],
        "seed": result.seed,
        "prompt": payload["prompt"],
        "negative": payload.get("negative_prompt", ""),
        "mode": payload.get("mode", "txt2img"),
        "favorite": 0,
        "flagged": 1 if flagged else 0,
        "elapsed_ms": result.elapsed_ms,
        "created_at": now_iso(),
    }
    image.close()
    return record


def _shared_reference(payload: dict):  # pragma: no cover - 兼容旧调用点
    """占位：当前每次调用独立解析参考图，故返回 None。"""
    return None


async def run_job(job_id: str, payload: dict | None = None) -> None:
    """执行一个任务（由调度器串行调用）。

    payload 为空时（队列消费场景），从数据库中的参数快照恢复。
    """
    if payload is None:
        job = repo.get_job(job_id)
        if not job:
            return
        try:
            payload = blobs.loads(job["params_json"])
        except Exception:
            repo.mark_job_done(job_id, "failed", error="任务参数损坏")
            await bus.publish(job_id, "error", {"status": "failed", "error": "任务参数损坏"})
            return
        payload["count"] = job.get("count", 1)

    repo.mark_job_running(job_id)
    await bus.publish(job_id, "status", {"status": "running", "progress": 1})

    count = int(payload.get("count", 1))
    loop = asyncio.get_running_loop()
    created: list[dict] = []

    try:
        # 显存预检
        ok, why = vram_guard(required_gb=2.0)
        if not ok and manager.mode == "local":
            manager.unload()
            dev.empty_cache()

        # 加载模型（可能耗时较长）
        await bus.publish(job_id, "progress", {"step": 0, "total": count, "percent": 2,
                                               "message": "加载模型..."})
        await loop.run_in_executor(None, manager.ensure_loaded)

        for index in range(count):
            def _cb(percent: int, message: str, _index=index) -> None:
                overall = int((( _index + percent / 100.0) / count) * 96) + 2
                asyncio.run_coroutine_threadsafe(
                    bus.publish(job_id, "progress", {
                        "step": _index + 1, "total": count,
                        "percent": overall, "message": message,
                    }),
                    loop,
                )
                repo.mark_job_progress(job_id, overall, message)

            record = await loop.run_in_executor(
                None, _run_once, payload, job_id, index, _cb
            )
            repo.insert_image(record)
            created.append(record)

            # 漫画分镜 / 场景概念图 / 角色形象图：把出图结果回写到对应记录
            panel_id = payload.get("comic_panel_id")
            scene_id = payload.get("comic_scene_id")
            character_id = payload.get("comic_character_id")
            if panel_id or scene_id or character_id:
                try:
                    from app.services import comic as comic_service

                    if panel_id:
                        comic_service.on_panel_image(panel_id, record)
                    if scene_id:
                        comic_service.on_scene_image(scene_id, record)
                    if character_id:
                        comic_service.on_character_image(character_id, record)
                except Exception:
                    pass

            await bus.publish(job_id, "image", {
                "id": record["id"],
                "url": record["url"],
                "seed": record["seed"],
                "width": record["width"],
                "height": record["height"],
                "elapsed_ms": record["elapsed_ms"],
                "flagged": record["flagged"],
            })

        repo.mark_job_done(job_id, "succeeded")
        manager.touch()
        await bus.publish(job_id, "done", {
            "status": "succeeded",
            "count": len(created),
            "images": [
                {"id": r["id"], "url": r["url"], "seed": r["seed"]} for r in created
            ],
        })

    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        import traceback as _traceback

        print(f"[出图失败] job={job_id}\n{_traceback.format_exc()}", flush=True)
        repo.mark_job_done(job_id, "failed", error=message)
        manager.last_error = message

        panel_id = (payload or {}).get("comic_panel_id")
        scene_id = (payload or {}).get("comic_scene_id")
        character_id = (payload or {}).get("comic_character_id")
        if panel_id or scene_id or character_id:
            try:
                from app.services import comic as comic_service

                if panel_id:
                    comic_service.on_panel_error(panel_id, message)
                if scene_id:
                    comic_service.on_scene_error(scene_id, message)
                if character_id:
                    comic_service.on_character_error(character_id, message)
            except Exception:
                pass

        await bus.publish(job_id, "error", {"status": "failed", "error": message})

    finally:
        # 任务已结束，参数里的参考图文件不再需要（成功失败都清）
        blobs.release(job_id)


async def generate_sync(payload: dict) -> dict:
    """同步出图：直接执行，不走队列（适合单张快速调用）。"""
    job_id = payload["job_id"]
    await run_job(job_id, payload)
    job = repo.get_job(job_id) or {}
    if job.get("status") != "succeeded":
        raise GenerationError(job.get("error") or "生成失败")
    return job


def estimate(payload: dict) -> dict:
    """粗略估算耗时与显存占用，供前端提示。"""
    width = payload.get("width", settings.default_width)
    height = payload.get("height", settings.default_height)
    steps = payload.get("steps", settings.default_steps)
    batch = payload.get("batch_size", 1)
    mode = manager.mode

    megapixels = (width * height) / 1_000_000

    if mode == "mock":
        seconds = steps * 0.02 * batch + 0.3
        vram = 0.0
    elif mode == "comfy":
        # RTX 2060 6GB + --lowvram 实测校准：约 260 秒/步/百万像素
        seconds = steps * megapixels * 260.0 * batch + 180  # +180s 首次模型加载
        vram = 4.6 + megapixels * 1.5
    else:
        # 基于 1024x1024/20 步约 10 秒的粗略线性模型，仅作提示
        base = 10.0
        scale = (megapixels / 1.048576) * (steps / 20.0)
        seconds = base * scale * batch
        vram = 4.6 + megapixels * 2.4

    warnings: list[str] = []
    if width % 64 or height % 64:
        warnings.append("尺寸必须是 64 的整数倍")
    if megapixels > 2.25:
        warnings.append("像素量较大，可能显存不足，建议开 VAE 分块或降低分辨率")
    if mode == "comfy" and seconds > settings.infer_timeout_seconds:
        warnings.append(
            f"预计耗时 {seconds / 60:.0f} 分钟，超过超时上限 {settings.infer_timeout_seconds // 60} 分钟，"
            "任务会被自动取消——请降低分辨率或步数"
        )
    total, free = dev.vram_info_gb()
    if total and free and vram > free:
        warnings.append(f"估算显存 {vram:.1f} GB 超过当前可用 {free:.1f} GB")

    return {
        "estimated_seconds": round(seconds, 2),
        "estimated_vram_gb": round(vram, 2),
        "megapixels": round(megapixels, 2),
        "warnings": warnings,
    }


async def submit_batch(payload: dict) -> dict:
    """提交异步任务。"""
    from app.inference import scheduler

    job_id = payload["job_id"]
    if not scheduler.try_reserve_slot():
        raise GenerationError("队列已满，请稍后重试")
    repo.create_job(
        job_id=job_id,
        prompt=payload["prompt"],
        negative=payload.get("negative_prompt", ""),
        params=payload,
        seed=payload.get("seed", -1),
        count=int(payload.get("count", 1)),
        mode=payload.get("mode", "txt2img"),
        status="queued",
    )
    queued = await scheduler.submit(job_id)
    if not queued:
        repo.mark_job_done(job_id, "failed", error="队列已满")
        raise GenerationError("队列已满，请稍后重试")

    qsize = scheduler.queue_size()
    return {"job_id": job_id, "status": "queued", "position": qsize, "queue_size": qsize}


def cleanup_outputs() -> dict:
    """按磁盘配额清理旧图（保留数据库记录，仅删文件并标记）。"""
    limit_gb = settings.max_output_dir_gb
    total = 0
    files: list[tuple[float, Path]] = []
    for p in settings.output_dir_path.rglob("*.png"):
        try:
            st = p.stat()
        except OSError:
            continue
        total += st.st_size
        files.append((st.st_mtime, p))

    removed = 0
    if total / 1024 ** 3 > limit_gb:
        files.sort()  # 最旧的先删
        for _, p in files:
            if total / 1024 ** 3 <= limit_gb:
                break
            try:
                size = p.stat().st_size
                p.unlink()
                total -= size
                removed += 1
            except OSError:
                continue
    return {"removed": removed, "used_gb": round(total / 1024 ** 3, 2)}
