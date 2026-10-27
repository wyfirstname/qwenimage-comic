# -*- coding: utf-8 -*-
"""动画帧：提示词、对齐、图集、元数据。

2.5D 斜 45° 的角色不能当立绘用——它得能走。这个模块负责把「一堆各自生成的帧」
收拾成一套**帧序稳定、脚底不抖、可直接进引擎**的精灵表：

1. 帧提示词：方向 + 帧序 + 姿态（站立呼吸循环 / 行走两拍循环）
2. 对齐：按 alpha 包围盒裁到内容，**整向统一缩放**（用该向 idle 首帧定标尺，逐帧各算
   会让角色走动时一胀一缩），再按「脚底锚点」贴进固定 cell
3. 图集：每个方向一张 PNG，第一行站立、第二行行走（行/列数写进元数据）
4. 元数据：cell / anchor / dirs / mirror / clips(fps,count) / banks(sheet,cols,rows)
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from .presets import CLIPS, DIRECTIONS, VIEW_LOCK

# 主体在 cell 里占的高度比例（留头顶与脚边余量，避免贴边被引擎切到）
_FILL = 0.86


def frame_suffix(clip: str, idx: int, total: int, dir_id: str) -> str:
    """拼在提示词尾部的动画修饰——姿态 + 朝向 + 一致性要求。"""
    poses = CLIPS.get(clip, {}).get("poses") or [""]
    pose = poses[min(idx, len(poses) - 1)]
    facing = DIRECTIONS.get(dir_id, {}).get("prompt", "")
    return (f"animation frame {idx + 1} of {total}: {pose}, {facing}, "
            "same character, identical costume and colors as the reference, "
            "identical proportions, feet planted on the same ground line")


def _fit_scale(im: Image.Image, cell: tuple[int, int]) -> float:
    """按内容高度算出该帧单独需要的缩放（只用于挑基准帧）。"""
    bb = im.getbbox()
    if not bb:
        return 1.0
    h = max(1, bb[3] - bb[1])
    return (cell[1] * _FILL) / h


def place(im: Image.Image, cell: tuple[int, int], anchor: tuple[int, int],
          scale: float) -> Image.Image:
    """裁到内容 → 按给定缩放 → 脚底中心对齐锚点 → 贴进 cell（越界自动回缩）。"""
    im = im.convert("RGBA")
    bb = im.getbbox()
    if bb:
        im = im.crop(bb)
    w, h = im.size
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    # 超出 cell 或高过锚点 → 等比回缩，宁可小一点也不裁掉
    shrink = min(1.0, cell[0] / nw, anchor[1] / nh)
    if shrink < 1.0:
        nw, nh = max(1, round(nw * shrink)), max(1, round(nh * shrink))
    im = im.resize((nw, nh), Image.LANCZOS)
    out = Image.new("RGBA", cell, (0, 0, 0, 0))
    x = max(0, min(cell[0] - nw, round(anchor[0] - nw / 2)))
    y = max(0, min(cell[1] - nh, anchor[1] - nh))
    out.alpha_composite(im, (x, y))
    return out


def compose_sheet(rows: list[tuple[str, list[Image.Image]]],
                  cell: tuple[int, int]) -> tuple[Image.Image, dict]:
    """把若干「行」拼成一张图集。行 = (clip 名, 该 clip 的 cell 图列表)。"""
    cols = max((len(fs) for _, fs in rows), default=1) or 1
    sheet = Image.new("RGBA", (cell[0] * cols, cell[1] * len(rows)), (0, 0, 0, 0))
    layout: dict[str, dict] = {}
    for r, (clip, frames) in enumerate(rows):
        for c, im in enumerate(frames):
            sheet.alpha_composite(im, (c * cell[0], r * cell[1]))
        layout[clip] = {"row": r, "count": len(frames)}
    return sheet, {"cols": cols, "rows": len(rows), "clips": layout}


def build_frames(raw: dict[str, list[Path]], cell: tuple[int, int],
                 anchor: tuple[int, int]) -> dict[str, list[Image.Image]]:
    """原始生成帧 → 对齐好的 cell 图。**整向统一缩放**：以第一条 clip 的第一帧定标尺。"""
    scale = None
    for clip in ("idle", "walk"):
        for p in raw.get(clip) or []:
            im = Image.open(p).convert("RGBA")
            if im.getbbox():
                scale = _fit_scale(im, cell)
            break
        if scale:
            break
    scale = scale or 1.0
    out: dict[str, list[Image.Image]] = {}
    for clip, paths in raw.items():
        out[clip] = [place(Image.open(p), cell, anchor, scale) for p in paths]
    return out


def save_bank(frames: dict[str, list[Image.Image]], dst: Path,
              cell: tuple[int, int]) -> dict:
    """落一张图集 PNG，返回元数据（相对 dst 的 url 由调用方补）。"""
    rows = [(clip, frames[clip]) for clip in ("idle", "walk") if frames.get(clip)]
    sheet, layout = compose_sheet(rows, cell)
    dst.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(dst)
    return {"file": dst.name, "size": list(sheet.size), **layout}


def clip_meta() -> dict:
    return {cid: {"name": c["name"], "fps": c["fps"], "poses": len(c["poses"])}
            for cid, c in CLIPS.items()}


def dir_meta(dir_id: str) -> dict:
    d = DIRECTIONS.get(dir_id, {})
    return {"id": dir_id, "name": d.get("name", dir_id),
            "short": d.get("short", dir_id.upper())}


def view_meta() -> dict:
    return {"id": VIEW_LOCK["id"], "name": VIEW_LOCK["name"],
            "cell": list(VIEW_LOCK["cell"]), "anchor": list(VIEW_LOCK["anchor"])}
