# -*- coding: utf-8 -*-
"""色调一致性后验校验。

提示词里的色调词只是「请求」，模型听不听要另说——所以每张素材出图后
把它和基准图都做主色提取（PIL quantize），算色板距离，给出可验证的
一致分。分高不代表图片好看，只代表色调确实贴着基准走。

距离口径：两组主色按占比降序，贪心最近匹配（不做全排列，6 色板 6! 太狠），
取平均 RGB 欧氏距离，0-255 量纲。经验阈值 48：同一时间背景下正常出图在
20-40 之间，明显跑色（白天出了夜景）会到 80+。阈值可按素材集覆盖。
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

DEFAULT_THRESHOLD = 48.0
_PALETTE_N = 6


def extract_palette(source, n: int = _PALETTE_N) -> list[dict]:
    """主色板：按像素占比降序。source 接受 bytes / Path。

    先缩到 64 边长再量化（quantize 在大图上又慢又被噪点带偏）。
    """
    if isinstance(source, (bytes, bytearray)):
        img = Image.open(io.BytesIO(source))
    else:
        img = Image.open(Path(source))
    img = img.convert("RGB").resize((64, 64), Image.BILINEAR)
    q = img.quantize(colors=n, method=Image.Quantize.MEDIANCUT)
    palette = q.getpalette() or []
    colors: list[tuple[int, int, int]] = [
        (palette[i * 3], palette[i * 3 + 1], palette[i * 3 + 2])
        for i in range(min(n, len(palette) // 3))
    ]
    counts = sorted(q.getcolors(), reverse=True)  # [(像素数, 索引)]
    out = []
    for cnt, idx in counts:
        if idx >= len(colors):
            continue
        r, g, b = colors[idx]
        out.append({"hex": f"#{r:02X}{g:02X}{b:02X}", "share": round(cnt / 4096, 3)})
    return out


def palette_distance(pa: list[dict], pb: list[dict]) -> float:
    """两组 hex 主色的平均最近距离（0-255）。空色板按完全不一致处理。"""
    if not pa or not pb:
        return 255.0
    rgb_a = [_hex_rgb(c["hex"]) for c in pa]
    rgb_b = [_hex_rgb(c["hex"]) for c in pb]
    rest = list(range(len(rgb_b)))
    dists = []
    for ra, ga, ba in rgb_a:
        best_i, best_d = -1, 1 << 30
        for i in rest:
            rb, gb, bb = rgb_b[i]
            d = (ra - rb) ** 2 + (ga - gb) ** 2 + (ba - bb) ** 2
            if d < best_d:
                best_i, best_d = i, d
        dists.append(best_d ** 0.5)
        rest.remove(best_i)
    return sum(dists) / len(dists)


def check(asset_source, ref_source, threshold: float = DEFAULT_THRESHOLD) -> dict:
    """色调一致性判定：asset 是否贴着基准 ref 的色板走。"""
    ref_colors = extract_palette(ref_source)
    asset_colors = extract_palette(asset_source)
    score = palette_distance(asset_colors, ref_colors)
    return {
        "score": round(score, 1),
        "pass": bool(score <= threshold),
        "threshold": threshold,
        "ref_colors": [c["hex"] for c in ref_colors],
        "asset_colors": [c["hex"] for c in asset_colors],
    }


def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
