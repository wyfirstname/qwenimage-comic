# -*- coding: utf-8 -*-
"""模型管理器：引擎选择、懒加载、空闲卸载、状态上报。"""

from __future__ import annotations

import threading
import time
from typing import Optional

from shared.config import settings
from engine.infer import device as dev
from engine.infer.base import BaseEngine
from engine.infer.mock_engine import MockEngine


class ModelManager:
    """统一管理推理引擎的生命周期。

    - ENGINE_MODE=mock  -> 占位引擎，无需模型与 torch
    - ENGINE_MODE=local -> 本地真实推理（GGUF）
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._engine: Optional[BaseEngine] = None
        self._loading = False
        self.last_error: Optional[str] = None
        self.last_load_seconds: Optional[float] = None
        self.last_used_at: float = 0.0
        self._register_slot()

    def _register_slot(self) -> None:
        """在显存槽位表里登记「出图」这一路。

        注册自己的 unload/is_loaded 而不是让仲裁层反过来调 manager，
        这样 slots.py 不需要 import 任何引擎，依赖保持单向。
        """
        from engine.slots import slots

        slots.register("image", unload=self.unload, is_loaded=self.is_loaded)

    # ---------------- 引擎构建 ----------------
    def _create_engine(self) -> BaseEngine:
        mode = (settings.engine_mode or "mock").lower()
        if mode == "local":
            from engine.infer.local_engine import LocalEngine

            return LocalEngine()  # type: ignore[return-value]
        if mode == "comfy":
            from engine.infer.comfy_engine import ComfyEngine

            return ComfyEngine()  # type: ignore[return-value]
        return MockEngine()  # type: ignore[return-value]

    @property
    def engine(self) -> Optional[BaseEngine]:
        return self._engine

    @property
    def mode(self) -> str:
        return (settings.engine_mode or "mock").lower()

    def is_loaded(self) -> bool:
        return bool(self._engine and self._engine.loaded)

    # ---------------- 加载 / 卸载 ----------------
    def ensure_loaded(self) -> None:
        """确保引擎已加载（线程安全）。

        先进槽位：如果刚才在跑旁白，这一步会先把 TTS 的显存收回来，
        否则下面 load() 会直接 OOM。
        """
        if settings.model_slots_enabled:
            from engine.slots import slots

            slots.acquire("image")
        with self._lock:
            if self._engine is None:
                self._engine = self._create_engine()
            if self._engine.loaded:
                self.last_used_at = time.time()
                return

            self._loading = True
            try:
                started = time.time()
                self._engine.load()
                self.last_load_seconds = round(time.time() - started, 2)
                self.last_error = None
                self.last_used_at = time.time()
            except Exception as exc:
                self.last_error = str(exc)
                raise
            finally:
                self._loading = False

    def unload(self) -> None:
        with self._lock:
            if self._engine is not None:
                try:
                    self._engine.unload()
                except Exception:
                    pass
            self._engine = None
            dev.empty_cache()

    def maybe_idle_unload(self) -> None:
        """空闲超时后释放显存。"""
        if settings.keep_loaded or not self.is_loaded():
            return
        if not self.last_used_at:
            return
        if time.time() - self.last_used_at > settings.idle_unload_seconds:
            self.unload()

    def touch(self) -> None:
        self.last_used_at = time.time()

    # ---------------- 卡死恢复 ----------------
    def recover_engine(self, hard: bool = True) -> tuple[bool, str]:
        """复位推理引擎，供「卡死恢复」使用。

        hard=False —— 软恢复：请求 ComfyUI 清队列 / 中断 / 释放显存；
        hard=True  —— 硬恢复：额外结束并重新拉起 ComfyUI 子进程（仅限本服务拉起的）。

        引擎尚未创建时也会先创建：此时没有卡住的连接，恢复等价于空操作，
        但能保证后续状态查询是准的。
        """
        with self._lock:
            if self._engine is None:
                self._engine = self._create_engine()
            engine = self._engine
        try:
            if hard:
                fn = getattr(engine, "hard_restart", None)
                if fn is None:
                    return True, "当前引擎不持有可重启的推理进程（mock/local），无需恢复"
                return fn()
            fn = getattr(engine, "interrupt_all", None)
            if fn is None:
                return True, "当前引擎无需中断"
            fn()
            return True, "已请求中断当前推理并释放显存"
        except Exception as exc:  # pragma: no cover - 平台相关
            self.last_error = str(exc)
            return False, f"引擎恢复失败：{exc}"

    # ---------------- 状态 ----------------
    def status(self) -> dict:
        total, free = dev.vram_info_gb()
        files = settings.model_files_status()
        note = None
        if self.mode == "mock":
            note = "当前为占位出图模式（ENGINE_MODE=mock）。改为 comfy 并安装 ComfyUI 后即为真实推理。"
        elif self.mode == "comfy":
            note = None if self.is_loaded() else "正在连接/启动 ComfyUI 推理引擎（首次冷启动约 1-2 分钟）..."
        elif not files.get("ready"):
            note = "模型文件不完整，加载将失败。请按文档第 4 节下载权重。"
        elif not self.is_loaded() and not self._loading:
            note = "服务启动时会自动在后台加载模型，无需手动操作；此处按钮仅用于手动重试/释放显存。"

        return {
            "engine_mode": self.mode,
            "loaded": self.is_loaded(),
            "loading": self._loading,
            "device": settings.device,
            "keep_loaded": settings.keep_loaded,
            "vram_total_gb": total,
            "vram_free_gb": free,
            "torch_available": dev.TORCH_AVAILABLE,
            "gpu_name": dev.gpu_name(),
            "model_files": files,
            "last_error": self.last_error,
            "last_load_seconds": self.last_load_seconds,
            "note": note,
        }


manager = ModelManager()
