# -*- coding: utf-8 -*-
"""显存槽位仲裁：单卡上「用哪个模型就装哪个，其余当场卸掉」。

问题
----
一块 16G 卡上，Qwen-Image（GGUF + Qwen3-VL 文本编码器）和 IndexTTS-2
不可能同时常驻。更糟的是两边都以为自己独占显存：出图把 TTS 顶掉，
TTS 又把出图顶掉，表现是「每次切换都重新加载」，用户看到的是几分钟的等待。

解法
----
把「谁占着显存」变成一个显式的、有名字的状态（槽位）：
    * acquire("image") → 先把 tts 卸掉，再把 image 标成活跃
    * acquire("tts")   → 先把 image 卸掉，再把 tts 标成活跃
    * 谁都不用的时间超过 slot_idle_unload_seconds → 全部卸掉，显存归零

三条设计约束（都是踩过坑才写进去的）
----------------------------------
1. **卸载回调必须在锁外执行**。manager.unload() 自己要拿 manager 的锁，
   如果这里持着槽位锁去调它，而它内部又回调槽位（未来可能会），
   就是一个教科书式的 ABBA 死锁。所以先收集、后执行。
2. **卸载失败不能阻断后续加载**。ComfyUI 的 /free 偶尔超时，
   TTS 进程可能已经被用户手动关掉——这些都不该让「加载新模型」失败。
3. **进程句柄放模块级**（和 comfy_engine 的教训一致）：卸载后实例被丢弃，
   句柄一丢就既杀不掉也拉不起来。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from shared.config import settings


@dataclass
class Slot:
    kind: str
    unload: Callable[[], None]
    is_loaded: Callable[[], bool]
    last_used: float = field(default=0.0)
    unload_count: int = 0

    def is_loaded_safe(self) -> bool:
        """探测函数自己也可能抛（进程已经死了 / 引擎重建中），不能让它带崩仲裁。"""
        try:
            return bool(self.is_loaded())
        except Exception:
            return False


class SlotManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._slots: dict[str, Slot] = {}
        self._active: str = ""
        self._watch: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ---------------- 注册 ----------------
    def register(self, kind: str, *, unload: Callable[[], None],
                 is_loaded: Callable[[], bool]) -> None:
        with self._lock:
            self._slots[kind] = Slot(kind=kind, unload=unload, is_loaded=is_loaded)

    def kinds(self) -> list[str]:
        with self._lock:
            return sorted(self._slots)

    # ---------------- 核心：抢槽位 ----------------
    def acquire(self, kind: str) -> dict:
        """声明「接下来要用 kind」。返回 {evicted: [...], active: kind}。

        真正的加载由调用方自己做（各引擎的加载方式差别太大，硬塞进这里
        只会长出一堆 if）。
        """
        if not settings.model_slots_enabled:
            self.touch(kind)
            return {"evicted": [], "active": kind, "disabled": True}

        to_unload: list[Slot] = []
        with self._lock:
            for k, slot in self._slots.items():
                if k == kind:
                    continue
                try:
                    loaded = bool(slot.is_loaded())
                except Exception:
                    loaded = False
                if loaded:
                    to_unload.append(slot)
            prev = self._active
            self._active = kind
            if kind in self._slots:
                self._slots[kind].last_used = time.time()

        # 锁外卸载（见模块注释第 1 条）
        evicted: list[str] = []
        for slot in to_unload:
            if self._do_unload(slot):
                evicted.append(slot.kind)
        if evicted or prev != kind:
            print(f"[slots] 切到 {kind}"
                  + (f"，已卸载 {', '.join(evicted)}" if evicted else ""), flush=True)
        return {"evicted": evicted, "active": kind}

    def touch(self, kind: str) -> None:
        """标记「刚刚用过」，用于闲时计时。"""
        with self._lock:
            slot = self._slots.get(kind)
            if slot is not None:
                slot.last_used = time.time()

    # ---------------- 卸载 ----------------
    def _do_unload(self, slot: Slot) -> bool:
        try:
            slot.unload()
            slot.unload_count += 1
            return True
        except Exception as exc:
            print(f"[slots] 卸载 {slot.kind} 失败（忽略）：{exc}", flush=True)
            return False

    def unload(self, kind: str) -> bool:
        with self._lock:
            slot = self._slots.get(kind)
            if slot is None:
                return False
            if self._active == kind:
                self._active = ""
        return bool(slot and self._do_unload(slot))

    def unload_all(self) -> list[str]:
        with self._lock:
            slots = list(self._slots.values())
            self._active = ""
        done = [s.kind for s in slots if s.is_loaded_safe() and self._do_unload(s)]
        return done

    # ---------------- 闲时卸载 ----------------
    def start_idle_watch(self) -> None:
        """起后台线程盯着空闲时间。重复调用无副作用。"""
        if not settings.model_slots_enabled:
            return
        with self._lock:
            if self._watch is not None and self._watch.is_alive():
                return
            self._stop.clear()
            self._watch = threading.Thread(target=self._idle_loop,
                                          name="slot-idle", daemon=True)
            self._watch.start()

    def stop_idle_watch(self) -> None:
        self._stop.set()
        with self._lock:
            self._watch = None

    def _idle_loop(self) -> None:
        idle = max(30, int(settings.slot_idle_unload_seconds))
        tick = max(5, min(30, idle // 4))
        while not self._stop.wait(tick):
            try:
                self._sweep(idle)
            except Exception:
                pass

    def _sweep(self, idle: float) -> None:
        now = time.time()
        victims: list[Slot] = []
        with self._lock:
            for slot in self._slots.values():
                if not slot.last_used:
                    continue
                if now - slot.last_used < idle:
                    continue
                try:
                    if slot.is_loaded():
                        victims.append(slot)
                        slot.last_used = 0.0
                except Exception:
                    pass
            if victims and self._active in {v.kind for v in victims}:
                self._active = ""
        for slot in victims:
            if self._do_unload(slot):
                print(f"[slots] {slot.kind} 空闲超过 {idle:.0f}s，已卸载释放显存",
                      flush=True)

    # ---------------- 状态 ----------------
    def status(self) -> dict:
        now = time.time()
        with self._lock:
            items = {}
            for k, s in self._slots.items():
                try:
                    loaded = bool(s.is_loaded())
                except Exception:
                    loaded = False
                items[k] = {
                    "loaded": loaded,
                    "idle_seconds": round(now - s.last_used, 1) if s.last_used else None,
                    "unload_count": s.unload_count,
                }
            return {
                "enabled": bool(settings.model_slots_enabled),
                "active": self._active,
                "idle_unload_seconds": int(settings.slot_idle_unload_seconds),
                "slots": items,
            }


def run_idle_watch_once(idle_seconds: Optional[float] = None) -> list[str]:
    """跑一次闲时扫描。给测试和「手动回收显存」按钮用。"""
    idle = float(idle_seconds if idle_seconds is not None
                 else max(30, int(settings.slot_idle_unload_seconds)))
    before = {k: v["loaded"] for k, v in slots.status()["slots"].items()}
    slots._sweep(idle)
    after = {k: v["loaded"] for k, v in slots.status()["slots"].items()}
    return [k for k in before if before[k] and not after.get(k)]


slots = SlotManager()

__all__ = ["slots", "run_idle_watch_once", "SlotManager", "Slot"]
