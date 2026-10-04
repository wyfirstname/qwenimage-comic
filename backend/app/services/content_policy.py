# -*- coding: utf-8 -*-
"""内容策略：仅做本地敏感词提示，不改变生成能力。

默认 flag_only：命中即打标记 + 记日志，图片照常返回，由使用者自行决定
是否启用 block。
"""

from __future__ import annotations

from app.config import settings

# 仅作示例的最小词表，使用者可按需扩充
_FLAG_WORDS = [
    "nsfw", "nude", "naked", "explicit", "gore", "bloodbath",
]


def evaluate(prompt: str, negative: str = "") -> tuple[bool, str]:
    """返回 (是否命中, 原因)。"""
    mode = (settings.nsfw_filter or "off").lower()
    if mode == "off":
        return False, ""

    text = f"{prompt} {negative}".lower()
    hits = [w for w in _FLAG_WORDS if w in text]
    if not hits:
        return False, ""
    return True, "命中标记词：" + ", ".join(hits)


def is_blocking() -> bool:
    return (settings.nsfw_filter or "").lower() == "block"
