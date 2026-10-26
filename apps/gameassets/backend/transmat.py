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

import os
from pathlib import Path

from PIL import Image

# transmat.py → backend → gameassets → apps → 仓库根
_ROOT = Path(__file__).resolve().parents[3]

# 色键参数：与纯白的距离（0-441 欧氏）
_CHROMA_NEAR = 40      # 完全透明
_CHROMA_FAR = 110      # 完全不保留
_FEATHER = 1           # alpha 边缘羽化半径（px）

# rembg 会话（首次调用时按需加载；模型缺失时由 rembg 自己下载）
_SESSION = None


def rembg_home() -> Path:
    """rembg 模型缓存目录。

    默认落在仓库/便携包自己的 models/rembg 下，而不是用户主目录的
    ~/.rembg —— 便携包要「整个文件夹拷走就能用」，模型不能留在 C 盘。
    可用环境变量 REMBG_HOME 覆盖。
    """
    p = Path(os.environ.get("REMBG_HOME") or (_ROOT / "models" / "rembg"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def rembg_model() -> str:
    """抠图模型名。默认 u2net（约 176MB、CPU 秒级）。"""
    return os.environ.get("REMBG_MODEL") or "u2net"


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
    out = remove(im, session=_session())
    _despill(out)
    out.save(dst, "PNG")
    return _report(out, "rembg")


def _session():
    """复用同一个 onnxruntime 会话（模型只加载一次，约 1-2 秒）。"""
    global _SESSION
    if _SESSION is None:
        os.environ.setdefault("REMBG_HOME", str(rembg_home()))
        from rembg import new_session
        _SESSION = new_session(rembg_model())
    return _SESSION


def _feather_filter():
    from PIL import ImageFilter
    return ImageFilter.GaussianBlur(_FEATHER)


def _despill(im: Image.Image) -> None:
    """半透明像素的 RGB 收敛到前景（消除白边）：按覆盖率压暗，去白晕。

    1024×1024 是百万像素级，纯 Python 逐点循环要 1-2 秒，这里走 numpy 向量化；
    没有 numpy 时退回循环实现。
    """
    w, h = im.size
    try:
        import numpy as np
    except ImportError:                                 # 兜底：无 numpy 环境
        px = im.load()
        for y in range(h):
            for x in range(w):
                r, g, b, a = px[x, y]
                if 0 < a < 255:
                    k = a / 255.0
                    px[x, y] = (int(r * k), int(g * k), int(b * k), a)
        return
    arr = np.asarray(im).astype("float32")
    alpha = arr[..., 3:4]
    arr[..., :3] = arr[..., :3] * (alpha / 255.0)       # 全不透明像素不变
    im.paste(Image.fromarray(arr.clip(0, 255).astype("uint8"), "RGBA"))


def _report(im: Image.Image, method: str) -> dict:
    a = im.getchannel("A")
    hist = a.histogram()
    transparent = sum(hist[:250])
    return {"method": method, "w": im.width, "h": im.height,
            "alpha_ratio": round(1 - transparent / (im.width * im.height), 3)}
