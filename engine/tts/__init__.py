# -*- coding: utf-8 -*-
"""旁白能力（IndexTTS-2）——独立进程 + 显存槽位。

对外只有 client 一个入口；调用方（apps/）不要 import 这里的内部模块。
"""

from engine.tts.client import (  # noqa: F401
    EDGE_VOICES,
    TtsError,
    available,
    install_hint,
    is_loaded,
    load,
    needs_vram,
    shutdown,
    status,
    synth,
    unload,
    voices,
)

__all__ = ["EDGE_VOICES", "TtsError", "available", "install_hint", "is_loaded",
           "load", "needs_vram", "shutdown", "status", "synth", "unload",
           "voices"]
