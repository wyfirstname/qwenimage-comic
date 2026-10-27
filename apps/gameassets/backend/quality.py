# -*- coding: utf-8 -*-
"""出图质量守门：黑图 / 纯色废图 / 跑色 / 假透明，四重检测。

背景：2.1 蒸馏模型对提示词敏感（夜晚措辞、引导系数都能把采样推进崩溃），
坏图形态是「近纯黑 / 近纯色、无结构」。此前只有色调评分，坏图照样入库——
现在出图后先过这里：不合格自动换 seed 重试，重试仍失败才标记给用户看。

判定口径（对 768² 或 1152×640 的图，64 边长采样后计算）：
  - 亮度：RGB 均值 < 16 → 黑图（正常夜景也在 30+，纯黑图 0-10）
  - 对比度：亮度标准差 < 10 → 无结构（纯色 / 渐变占满）
  - 信息熵：灰度熵 < 1.8 bit → 低信息量（大块纯色）
  - 色调：沿用 tonecheck 色板距离，超阈 → 跑色（季节卡阈值由调用方放宽）
  - 透明：alpha_check（抠图后调用）→ 没通道/全不透明=假透明/裁边
"""

from __future__ import annotations

import io
import math
from pathlib import Path

from PIL import Image

from . import tonecheck

BRIGHT_MIN = 16.0      # 亮度下限
CONTRAST_MIN = 10.0    # 亮度标准差下限
ENTROPY_MIN = 1.8      # 灰度熵下限（bit）
TRANSPARENT_MIN = 0.01   # 要求透明的素材：透明像素占比下限（假透明检测）


def alpha_check(path) -> dict:
    """透明通道体检（借力 create-game-assets 的 asset_report.py --require-alpha 思路）。

    检出三类问题：
      - no_alpha      —— 文件没有 alpha 通道
      - fake_trans    —— RGBA 但所有像素全不透明（烤死背景/假棋盘格的典型形态）
      - edge_touch    —— 内容包围盒四边全贴到画框（主体可能被裁边）
    返回 {has_alpha, transparent_ratio, content_bounds, problems:[code,...], details}
    """
    from PIL import Image

    img = Image.open(Path(path))
    img.load()
    problems: list[str] = []
    if "A" not in img.getbands() and "transparency" not in img.info:
        return {"has_alpha": False, "transparent_ratio": 0.0,
                "content_bounds": None, "problems": ["no_alpha"],
                "details": {"format": img.format, "mode": img.mode}}
    rgba = img.convert("RGBA")
    alpha = rgba.getchannel("A")
    hist = alpha.histogram()
    n = rgba.width * rgba.height
    transparent = sum(hist[:200])            # alpha<200 视为透明/半透明
    ratio = transparent / max(1, n)
    bounds = alpha.getbbox()
    if ratio < TRANSPARENT_MIN:
        problems.append("fake_trans")
    if bounds and bounds == (0, 0, rgba.width, rgba.height):
        problems.append("edge_touch")        # 四边全贴框 → 可能裁边
    return {"has_alpha": True, "transparent_ratio": round(ratio, 4),
            "content_bounds": list(bounds) if bounds else None,
            "problems": problems,
            "details": {"format": img.format, "mode": img.mode,
                        "size": [rgba.width, rgba.height]}}


def _metrics(source) -> dict:
    if isinstance(source, (bytes, bytearray)):
        img = Image.open(io.BytesIO(source))
    else:
        img = Image.open(Path(source))
    img = img.convert("RGB").resize((64, 64), Image.BILINEAR)
    px = list(img.getdata())
    lum = [0.299 * r + 0.587 * g + 0.114 * b for r, g, b in px]
    mean = sum(lum) / len(lum)
    var = sum((v - mean) ** 2 for v in lum) / len(lum)
    # 灰度熵：64 级直方图
    hist = [0] * 64
    for v in lum:
        hist[min(63, int(v) // 4)] += 1
    n = len(lum)
    ent = 0.0
    for c in hist:
        if c:
            pf = c / n
            ent -= pf * math.log2(pf)
    return {"brightness": round(mean, 1),
            "contrast": round(var ** 0.5, 1),
            "entropy": round(ent, 2)}


def _log2(p: float) -> float:
    import math
    return math.log2(p)


def inspect(asset_source, ref_source, threshold: float = tonecheck.DEFAULT_THRESHOLD) -> dict:
    """综合判定。ref_source=None 时只做结构检测（基准图自身没有色调参照）。
    返回 {pass, reasons, metrics, tone}。tone 沿用 tonecheck 结构。"""
    m = _metrics(asset_source)
    tone = (tonecheck.check(asset_source, ref_source, threshold=threshold)
            if ref_source else {"score": 0.0, "pass": True,
                                "threshold": threshold, "ref_colors": [],
                                "asset_colors": []})
    reasons = []
    if m["brightness"] < BRIGHT_MIN:
        reasons.append(f"黑图（亮度 {m['brightness']:.0f} < {BRIGHT_MIN:.0f}）")
    if m["contrast"] < CONTRAST_MIN:
        reasons.append(f"无结构（对比度 {m['contrast']:.0f} < {CONTRAST_MIN:.0f}）")
    if m["entropy"] < ENTROPY_MIN:
        reasons.append(f"低信息量（熵 {m['entropy']:.2f} < {ENTROPY_MIN}）")
    if not tone["pass"]:
        reasons.append(f"跑色（色距 {tone['score']:.0f} > 阈值 {threshold:.0f}）")
    return {"pass": not reasons, "reasons": reasons, "metrics": m, "tone": tone}
