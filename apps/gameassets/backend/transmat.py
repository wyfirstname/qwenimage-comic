# -*- coding: utf-8 -*-
"""透明底处理：白底色键抠图（chroma）与 rembg（U2Net）双方案。

设计：
  - chroma：生成时提示词已强制「isolated on plain pure white background」，
    出图后把近白像素抠成透明。零依赖、零额外耗时，轮廓硬朗的主体
    （武器 / 建筑 / 图标）效果很好；毛发类边缘略生硬。
  - rembg：AI 抠图（U2Net，onnxruntime），毛发边缘自然，但需要额外
    ~176MB 模型与依赖，每张多 2-4 秒 CPU。懒加载：没装依赖时给出
    明确报错，不悄悄退化。

两方案都做一次「边缘去白晕」：对 alpha<255 的像素把 RGB 往前景色收敛，
避免导出到深色底上出现白边。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

# 色键参数：与纯白的距离（0-441 欧氏）
_CHROMA_NEAR = 40      # 完全透明
_CHROMA_FAR = 110      # 完全不保留
_FEATHER = 1           # alpha 边缘羽化半径（px）


def make_transparent(src: str | Path, dst: str | Path, method: str = "chroma") -> dict:
    """src → dst 透明 PNG。返回 {w,h,method,alpha_ratio}。"""
    if method == "rembg":
        return _via_rembg(src, dst)
    if method == "chroma":
        return _via_chroma(src, dst)
    raise ValueError(f"未知抠图方式：{method}（可选 chroma | rembg）")


def _via_chroma(src, dst) -> dict:
    im = Image.open(src).convert("RGB")
    try:
        import numpy as np
        arr = np.asarray(im).astype("float32")        # H,W,3（int16 平方会溢出）
        d = np.sqrt(((255.0 - arr) ** 2).sum(axis=2))  # 与纯白的距离
        alpha = np.clip((d - _CHROMA_NEAR) / (_CHROMA_FAR - _CHROMA_NEAR),
                        0.0, 1.0)
        alpha = (alpha * 255).astype("uint8")
        a_img = Image.fromarray(alpha, "L").filter(_feather_filter())
        out = Image.merge("RGBA", (*im.split(), a_img))
    except ImportError:                                # 无 numpy 的兜底
        w, h = im.size
        alpha = Image.new("L", (w, h), 255)
        ap, ip = alpha.load(), im.load()
        for y in range(h):
            for x in range(w):
                r, g, b = ip[x, y]
                dd = ((255 - r) ** 2 + (255 - g) ** 2 + (255 - b) ** 2) ** 0.5
                if dd <= _CHROMA_NEAR:
                    ap[x, y] = 0
                elif dd < _CHROMA_FAR:
                    ap[x, y] = int(255 * (dd - _CHROMA_NEAR) / (_CHROMA_FAR - _CHROMA_NEAR))
        alpha = alpha.filter(_feather_filter())
        out = Image.merge("RGBA", (*im.split(), alpha))
    _despill(out)
    out.save(dst, "PNG")
    return _report(out, "chroma")


def _via_rembg(src, dst) -> dict:
    try:
        from rembg import remove
    except ImportError as exc:
        raise RuntimeError(
            "rembg 未安装（pip install rembg onnxruntime），"
            "或改用 chroma 色键方案") from exc
    im = Image.open(src).convert("RGBA")
    out = remove(im)
    _despill(out)
    out.save(dst, "PNG")
    return _report(out, "rembg")


def _feather_filter():
    from PIL import ImageFilter
    return ImageFilter.GaussianBlur(_FEATHER)


def _despill(im: Image.Image) -> None:
    """半透明像素的 RGB 收敛到前景（消除白边）：alpha 越低，越靠近最近的实心像素色。"""
    px = im.load()
    w, h = im.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if 0 < a < 255:
                # 简单去白：往「白距离最远」的方向压（等价于乘以覆盖率）
                k = a / 255.0
                px[x, y] = (int(r * k), int(g * k), int(b * k), a)


def _report(im: Image.Image, method: str) -> dict:
    a = im.getchannel("A")
    hist = a.histogram()
    transparent = sum(hist[:250])
    return {"method": method, "w": im.width, "h": im.height,
            "alpha_ratio": round(1 - transparent / (im.width * im.height), 3)}
