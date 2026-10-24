# -*- coding: utf-8 -*-
"""风格预设：每个风格是一段固定的「风格基因」提示词。

一致性第二根支柱：同一素材集内所有图（基准/场景/角色/道具）都拼上
同一份风格基因 + 同一份时间背景色调，谁都不许自带私货。
"""

from __future__ import annotations

STYLE_PRESETS: dict[str, dict] = {
    "ghibli": {
        "id": "ghibli",
        "name": "吉卜力水彩",
        "prompt": ("ghibli style watercolor illustration, soft painterly brush strokes, "
                    "pastel palette, gentle warm light, hand-drawn anime aesthetic"),
        "negative": "photorealistic, 3d render, pixel art, harsh outlines",
        "swatch": ["#8FD0F5", "#A8D8A8", "#F5E6B8", "#E8A87C"],
    },
    "pixel": {
        "id": "pixel",
        "name": "像素复古",
        "prompt": ("16-bit pixel art game asset, crisp pixel clusters, limited retro palette, "
                    "clean dithering, snes jrpg style"),
        "negative": "photorealistic, smooth gradients, blurry, painterly",
        "swatch": ["#3B6EF5", "#43C59E", "#F7D31E", "#E85A7B"],
    },
    "cyberpunk": {
        "id": "cyberpunk",
        "name": "赛博霓虹",
        "prompt": ("cyberpunk concept art, neon rim light, rain-slick reflective surfaces, "
                    "high contrast cyan and magenta glow, cinematic sci-fi atmosphere"),
        "negative": "rustic, pastoral, watercolor, hand-drawn",
        "swatch": ["#00E5FF", "#E85AD5", "#141A2E", "#F5D90A"],
    },
    "lowpoly": {
        "id": "lowpoly",
        "name": "低多边形",
        "prompt": ("low poly 3d game art, flat shading, faceted geometry, "
                    "clean vector-like shapes, soft ambient occlusion"),
        "negative": "photorealistic, painterly, detailed textures, noise",
        "swatch": ["#7CB342", "#5AA7E8", "#F5B041", "#E57373"],
    },
    "darkfantasy": {
        "id": "darkfantasy",
        "name": "暗黑幻想",
        "prompt": ("dark fantasy game concept art, moody dramatic lighting, rich deep colors, "
                    "ornate details, epic oil painting texture"),
        "negative": "cute, chibi, pastel colors, flat cartoon",
        "swatch": ["#4E342E", "#8E2A3C", "#2E5E4A", "#C3A56B"],
    },
    "cel": {
        "id": "cel",
        "name": "卡通赛璐璐",
        "prompt": ("cel shaded cartoon game art, bold clean outlines, flat vivid color fills, "
                    "simple two-tone shadows, playful stylized shapes"),
        "negative": "photorealistic, painterly, grainy texture, dark gritty",
        "swatch": ["#FF8A65", "#4FC3F7", "#FFD54F", "#BA68C8"],
    },
}


def get_style(style_id: str) -> dict:
    if style_id not in STYLE_PRESETS:
        raise KeyError(f"未知风格：{style_id}（可选：{', '.join(STYLE_PRESETS)}）")
    return STYLE_PRESETS[style_id]
