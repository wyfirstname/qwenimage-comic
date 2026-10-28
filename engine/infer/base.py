# -*- coding: utf-8 -*-
"""推理引擎抽象层。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from PIL import Image


@dataclass
class EngineResult:
    image: Image.Image
    seed: int
    elapsed_ms: int
    engine: str = ""
    # 控制图是否真的进了模型（mock 用它证明「骨架到达了引擎」，comfy 用它记录通路）
    control: Optional[dict] = None


class BaseEngine(Protocol):
    """引擎协议：mock 与 local 引擎都需实现。"""

    name: str

    def load(self) -> None: ...

    def unload(self) -> None: ...

    @property
    def loaded(self) -> bool: ...

    def generate(
        self,
        *,
        prompt: str,
        negative: str = "",
        width: int = 1024,
        height: int = 1024,
        steps: int = 20,
        guidance_scale: float = 4.0,
        seed: int = -1,
        reference: Optional[Image.Image] = None,
        references: Optional[list[Image.Image]] = None,
        strength: float = 0.6,
        control: Optional[Image.Image] = None,
        control_mode: str = "pose",
        control_strength: float = 0.8,
        control_start: float = 0.0,
        control_end: float = 0.9,
        progress_cb: Optional[Callable[[int, str], None]] = None,
    ) -> EngineResult: ...
