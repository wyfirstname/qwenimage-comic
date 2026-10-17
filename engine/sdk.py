# -*- coding: utf-8 -*-
"""引擎对外的唯一门面（SDK）。

规则（重构后的铁律）：
    * apps/ 里的任何代码**只允许**通过 `from engine.sdk import engine` 使用模型能力；
    * 严禁直接 `import engine.infer.*` / `engine.generation`；
    * engine 层不许 import 任何 apps。

门面把「模型生命周期 + 出图 + 文本生成」收敛成一组稳定方法，这样将来引擎
换成独立进程（HTTP 传输）时，应用代码一行都不用改——换的是门面内部的后端。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from shared.config import settings


# ------------------------------------------------------------------
#  惰性取用引擎内部件（保持 import 轻量，且不把 torch 拖进应用进程导入期）
# ------------------------------------------------------------------
def _manager():
    from engine.infer.manager import manager

    return manager


def _scheduler():
    from engine.infer import scheduler

    return scheduler


def _generation():
    from engine import generation

    return generation


def _text():
    from engine.infer.comfy_text import text_engine

    return text_engine


def __getattr__(name: str):  # PEP 562：异常类也惰性暴露
    if name == "GenerationError":
        from engine.generation import GenerationError

        return GenerationError
    if name == "LocalTextError":
        from engine.infer.comfy_text import LocalTextError

        return LocalTextError
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class Engine:
    """应用视角下的「模型」：所有调用都落到引擎层，应用不持有任何权重。"""

    # ---------------- 生命周期（由平台/独立进程调用）----------------
    async def startup(self) -> None:
        """启动引擎。独立引擎进程（http 传输）无需在此重复启动。"""
        if settings.engine_mode_is_remote():
            return
        from engine import bootstrap

        await bootstrap.startup()

    async def shutdown(self) -> None:
        if settings.engine_mode_is_remote():
            return
        from engine import bootstrap

        await bootstrap.shutdown()

    # ---------------- 状态与生命周期 ----------------
    @property
    def mode(self) -> str:
        """引擎运行模式：mock | local | comfy。"""
        return _manager().mode

    def status(self) -> dict:
        return _manager().status()

    def ensure_loaded(self) -> None:
        _manager().ensure_loaded()

    def load(self) -> None:
        _manager().ensure_loaded()

    def unload(self) -> None:
        _manager().unload()

    def recover(self, hard: bool = True) -> tuple[bool, str]:
        """复位引擎（卡死恢复）。返回 (是否成功, 说明)。"""
        return _manager().recover_engine(hard=hard)

    def cancel_current(self) -> None:
        """中断当前正在跑的推理（不改数据库状态）。"""
        mgr = _manager()
        eng = mgr.engine
        if eng is not None and hasattr(eng, "cancel"):
            eng.cancel()

    # ---------------- 队列 / 显存 ----------------
    def queue_size(self) -> int:
        return _scheduler().queue_size()

    def running_job(self) -> Optional[str]:
        """当前正在执行的任务 id（没有则 None）。"""
        return _scheduler().running_job()

    def active_jobs(self) -> int:
        from shared import repo

        return repo.count_active_jobs()

    def vram_guard(self, required_gb: float) -> tuple[bool, str]:
        return _scheduler().vram_guard(required_gb)

    # ---------------- 出图 ----------------
    async def submit_batch(self, payload: dict) -> dict:
        """提交异步出图任务（进引擎队列，返回 {job_id, status, position, ...}）。"""
        return await _generation().submit_batch(payload)

    async def generate_sync(self, payload: dict) -> dict:
        """同步出图（阻塞到完成，适合单张）。"""
        return await _generation().generate_sync(payload)

    def estimate(self, payload: dict) -> dict:
        """估算耗时 / 显存，供界面提示。"""
        return _generation().estimate(payload)

    def register_job_hook(self, fn: Callable[[dict, dict, bool], None]) -> None:
        """注册出图回调 fn(payload, result, ok)：出图完成/失败时通知业务侧。"""
        _generation().register_job_hook(fn)

    # ---------------- 文本生成（复用 Qwen3-VL 文本编码器）----------------
    def text_generate(self, prompt: str, *, system: str = "",
                      max_length: Optional[int] = None,
                      temperature: Optional[float] = None,
                      on_progress: Optional[Callable[[float], None]] = None) -> str:
        return _text().generate(prompt, system=system, max_length=max_length,
                                temperature=temperature, on_progress=on_progress)

    def text_cancel(self) -> None:
        _text().cancel()

    def text_status(self) -> dict:
        return _text().status()

    @property
    def text_last_seconds(self) -> Optional[float]:
        return _text().last_seconds

    # ---------------- 旁白（IndexTTS，独立进程）----------------
    def tts_available(self) -> tuple[bool, str]:
        """能不能念旁白（不启动进程）。返回 (可用, 不可用原因)。"""
        from engine.tts import available

        return available()

    def tts_status(self) -> dict:
        from engine.tts import status

        return status()

    def tts_synth(self, text: str, out_path, *, speaker: str = "",
                  emo_wav: str = "", emo_alpha: Optional[float] = None,
                  emo_text: str = "", lang: str = "", speed: float = 0.0) -> dict:
        """把一句话念成 wav。会自动抢显存槽位。"""
        from engine.tts import synth

        return synth(text, out_path, speaker=speaker, emo_wav=emo_wav,
                     emo_alpha=emo_alpha, emo_text=emo_text, lang=lang,
                     speed=speed)

    def tts_load(self) -> dict:
        from engine.tts import load

        return load()

    def tts_unload(self) -> None:
        from engine.tts import unload

        unload()

    def tts_shutdown(self) -> None:
        from engine.tts import shutdown

        shutdown()

    # ---------------- 显存槽位 ----------------
    def slots_status(self) -> dict:
        from engine.slots import slots

        return slots.status()

    def unload_idle(self, seconds: Optional[float] = None) -> list[str]:
        """立刻回收空闲模型的显存（界面上的「释放显存」按钮）。"""
        from engine.slots import run_idle_watch_once

        return run_idle_watch_once(seconds if seconds is not None else 0.0)

    def unload_all(self) -> list[str]:
        from engine.slots import slots

        return slots.unload_all()


engine = Engine()

__all__ = ["engine", "Engine", "GenerationError", "LocalTextError"]
