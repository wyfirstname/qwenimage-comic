# -*- coding: utf-8 -*-
"""推理调度器：全局串行 + 显存守卫。

单卡环境下并发出图极易 OOM，因此所有推理请求都通过同一把锁串行执行。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable

from app.config import settings
from app.inference import device as dev
from app.inference.manager import manager

# 全局任务队列（异步），保证「先进先出 + 限流」
_job_queue: "asyncio.Queue[str]" | None = None
_queue_lock = threading.Lock()
_infer_lock = asyncio.Lock()
_running_job: str | None = None
_queue_size_hint = 0


def get_queue() -> "asyncio.Queue[str]":
    global _job_queue
    if _job_queue is None:
        _job_queue = asyncio.Queue(maxsize=max(1, settings.max_queue_size))
    return _job_queue


def queue_size() -> int:
    q = get_queue()
    return q.qsize()


def running_job() -> str | None:
    return _running_job


def try_reserve_slot() -> bool:
    """尝试占用一个队列位（用于提交前快速失败）。"""
    q = get_queue()
    return not q.full()


async def submit(job_id: str) -> bool:
    q = get_queue()
    try:
        q.put_nowait(job_id)
        return True
    except asyncio.QueueFull:
        return False


async def consume_loop(handler: Callable[[str], Any]) -> None:
    """后台协程：串行消费队列中的任务。"""
    global _running_job
    q = get_queue()
    while True:
        job_id = await q.get()
        _running_job = job_id
        try:
            await handler(job_id)
        except Exception:  # handler 内部已处理异常，这里兜底避免循环中断
            pass
        finally:
            _running_job = None
            q.task_done()
            manager.maybe_idle_unload()


def vram_guard(required_gb: float = 1.0) -> tuple[bool, str]:
    """出图前的显存检查。"""
    if not dev.cuda_available() or settings.device.startswith("cpu"):
        return True, ""
    free = dev.free_vram_gb()
    if free and free < required_gb:
        return False, f"可用显存不足（剩余 {free:.2f} GB）"
    return True, ""


def inference_lock() -> asyncio.Lock:
    return _infer_lock
