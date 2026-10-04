# -*- coding: utf-8 -*-
"""占位出图引擎（mock）。

用途：
1. 在未下载模型 / 未安装 torch 时，让整条链路（API -> 队列 -> 落盘 -> 图库）可跑通；
2. 前端与接口联调时提供稳定的、可预期的图片。

实现方式：根据 seed + 提示词生成确定性噪声图，叠加提示词文本与参数，
并绘制进度式色块，视觉上可区分不同参数。
"""

from __future__ import annotations

import hashlib
import time
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageFont

from app.inference.base import EngineResult


def _seed_from(text: str, seed: int) -> int:
    raw = f"{text}|{seed}".encode("utf-8")
    return int(hashlib.md5(raw).hexdigest()[:8], 16)


def _palette(seed: int) -> list[tuple[int, int, int]]:
    """从 seed 派生一组和谐的渐变色。"""
    base = seed % 360
    colors = []
    for i in range(3):
        h = (base + i * 47) % 360
        # HSV -> RGB（简化实现，避免额外依赖）
        c = 0.85
        x = c * (1 - abs((h / 60.0) % 2 - 1))
        m = 0.15
        if h < 60:
            r, g, b = c, x, 0
        elif h < 120:
            r, g, b = x, c, 0
        elif h < 180:
            r, g, b = 0, c, x
        elif h < 240:
            r, g, b = 0, x, c
        elif h < 300:
            r, g, b = x, 0, c
        else:
            r, g, b = c, 0, x
        colors.append((int((r + m) * 255), int((g + m) * 255), int((b + m) * 255)))
    return colors


def _load_font(size: int) -> ImageFont.ImageFont:
    """尝试加载中文字体，失败则退回内置位图字体。"""
    candidates = [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


class MockEngine:
    """占位引擎，接口与真实引擎保持一致。"""

    name = "mock"

    def load(self) -> None:
        time.sleep(0.2)  # 模拟一点加载耗时，让前端的加载态可见

    def unload(self) -> None:
        return None

    @property
    def loaded(self) -> bool:
        return True

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
        progress_cb: Optional[Callable[[int, str], None]] = None,
    ) -> EngineResult:
        started = time.time()
        actual_seed = seed if seed is not None and seed >= 0 else _seed_from(prompt, int(time.time()))
        colors = _palette(_seed_from(prompt, actual_seed))

        # 逐段推进，让前端进度条真实动起来
        total_steps = max(1, int(steps))
        for i in range(1, total_steps + 1):
            if progress_cb:
                progress_cb(int(i / total_steps * 90), f"采样 {i}/{total_steps}")
            time.sleep(0.02)

        first_ref = reference
        if first_ref is None and references:
            first_ref = references[0]
        if first_ref is not None:
            base = first_ref.convert("RGB").resize((width, height), Image.LANCZOS)
        else:
            base = Image.new("RGB", (width, height), colors[0])

        img = base.copy()
        draw = ImageDraw.Draw(img, "RGBA")

        # 斜向渐变叠加
        band = max(1, height // 64)
        for y in range(0, height, band):
            t = y / height
            i = min(len(colors) - 1, int(t * len(colors)))
            draw.rectangle([0, y, width, y + band], fill=(*colors[i], 60))

        # 噪点纹理，让每张图有差异
        rnd = _seed_from(prompt, actual_seed)
        px = img.load()
        step_px = max(2, min(width, height) // 160)
        for y in range(0, height, step_px):
            for x in range(0, width, step_px):
                rnd = (rnd * 1103515245 + 12345) & 0x7FFFFFFF
                v = (rnd >> 16) % 70
                r, g, b = px[x, y]
                px[x, y] = (
                    min(255, r + v),
                    min(255, g + v),
                    min(255, b + v),
                )

        # 叠加文字信息
        font_big = _load_font(max(18, width // 26))
        font_small = _load_font(max(13, width // 46))
        margin = max(16, width // 40)

        badge = "MOCK PREVIEW"
        draw.rectangle(
            [margin, margin, margin + len(badge) * (font_small.size // 1.2) + 24, margin + font_small.size + 16],
            fill=(0, 0, 0, 150),
        )
        draw.text((margin + 12, margin + 8), badge, font=font_small, fill=(255, 255, 255, 230))

        lines = _wrap(prompt, 28)[:4]
        text_y = height - margin - (len(lines) + 2) * (font_big.size + 8)
        draw.rectangle([0, text_y - 12, width, height], fill=(0, 0, 0, 140))
        for idx, line in enumerate(lines):
            draw.text((margin, text_y + idx * (font_big.size + 8)), line, font=font_big, fill=(255, 255, 255, 240))

        meta = f"{width}x{height} | steps {steps} | cfg {guidance_scale} | seed {actual_seed}"
        draw.text(
            (margin, text_y + len(lines) * (font_big.size + 8) + 4),
            meta,
            font=font_small,
            fill=(220, 220, 220, 220),
        )

        if progress_cb:
            progress_cb(95, "解码")
        time.sleep(0.05)

        return EngineResult(
            image=img,
            seed=actual_seed,
            elapsed_ms=int((time.time() - started) * 1000),
            engine=self.name,
        )


def _wrap(text: str, width: int) -> list[str]:
    """按字符数粗略换行，中英文混排也能用。"""
    text = (text or "").strip()
    if not text:
        return ["(empty prompt)"]
    return [text[i:i + width] for i in range(0, len(text), width)]
