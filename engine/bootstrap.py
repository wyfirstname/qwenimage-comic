# -*- coding: utf-8 -*-
"""引擎自身的启停。

引擎是「最底下一层」，它自己管自己的生命周期：建表（核心表）、清残留任务、
拉起调度消费循环、按需自动加载模型。上层（门户 / 应用的独立进程）只调用
`engine.sdk.engine.startup()` / `shutdown()`，不关心里面的细节。
"""

from __future__ import annotations

import asyncio
from typing import Optional

from shared import blobs, repo
from shared.config import settings
from shared.db import db
from engine.events import bus  # noqa: F401  （导入即建事件总线）
from engine.infer import device as dev
from engine.infer import scheduler
from engine.infer.manager import manager
from engine import generation

_consumer: Optional[asyncio.Task] = None
_auto_load: Optional[asyncio.Task] = None


async def startup() -> None:
    """启动引擎：建表、清残留、拉起消费循环、后台自动加载模型。"""
    global _consumer, _auto_load

    settings.ensure_dirs()
    db.init_schema()

    # 上次异常退出遗留的中间态任务：判为中断（可重试）
    for job in repo.list_jobs(limit=200, status="running"):
        repo.mark_job_done(job["id"], "failed", error="服务重启导致中断")
    for job in repo.list_jobs(limit=200, status="queued"):
        repo.mark_job_done(job["id"], "failed", error="服务重启导致中断")

    # 任务参数里的参考图临时文件此刻全部作废（排队中的任务已判失败）
    try:
        removed = blobs.gc_all()
        if removed:
            print(f"[engine] 清理任务参数临时文件 {removed} 份")
    except Exception as exc:  # pragma: no cover - 防御性
        print(f"[engine] 清理任务参数临时文件跳过：{exc}")

    if _consumer is None or _consumer.done():
        _consumer = asyncio.create_task(scheduler.consume_loop(generation.run_job))

    print(f"[engine] 运行模式 ENGINE_MODE={manager.mode}")
    print(f"[engine] 数据目录 {settings.data_dir_path}")
    print(f"[engine] 输出目录 {settings.output_dir_path}")

    # local/comfy 模式：启动即在后台自动连接/加载，无需手动点「加载」
    if manager.mode in ("local", "comfy"):
        files = settings.model_files_status()
        if not files.get("ready"):
            print("[engine] 模型文件不完整，跳过自动加载（详见界面「模型下载」）")
        elif not dev.TORCH_AVAILABLE:
            print("[engine] 未安装 torch 推理依赖，跳过自动加载")
        else:
            async def _load() -> None:
                loop = asyncio.get_running_loop()
                print("[engine] 正在后台自动加载模型（首次约 1-3 分钟）...")
                try:
                    await loop.run_in_executor(None, manager.ensure_loaded)
                    print(f"[engine] 模型自动加载完成，耗时 {manager.last_load_seconds}s")
                except Exception as exc:
                    print(f"[engine] 模型自动加载失败：{exc}")
                    print("        生成请求会再次尝试加载；持续失败请看模型状态里的报错。")

            if _auto_load is None or _auto_load.done():
                _auto_load = asyncio.create_task(_load())


async def shutdown() -> None:
    """关闭引擎：停掉消费循环、释放显存。"""
    global _consumer
    if _consumer is not None:
        _consumer.cancel()
        try:
            await _consumer
        except (asyncio.CancelledError, Exception):
            pass
        _consumer = None
    try:
        manager.unload()
    except Exception:
        pass
    print("[engine] 已释放资源")
