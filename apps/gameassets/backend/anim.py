# -*- coding: utf-8 -*-
"""动画帧：提示词、对齐、图集、元数据。

2.5D 斜 45° 的角色不能当立绘用——它得能走。这个模块负责把「一堆各自生成的帧」
收拾成一套**帧序稳定、脚底不抖、可直接进引擎**的精灵表：

1. 帧提示词：方向 + 帧序 + 姿态（站立呼吸 / 行走八帧 / 奔跑八帧）
2. 对齐：按 alpha 包围盒裁到内容，**整向统一缩放**（用该向 idle 首帧定标尺，逐帧各算
   会让角色走动时一胀一缩），再按「脚底锚点」贴进固定 cell
3. 图集：每个方向一张 PNG，行序 = 站立 / 行走 / 奔跑（行/列数写进元数据）
4. 元数据：cell / anchor / dirs / mirror / clips(fps,count) / banks(sheet,cols,rows)
5. 经典整表：行=朝向、列=步态帧，walk / run 各一张（大话西游式）
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from .presets import CLIP_ORDER, CLIPS, DIRECTIONS, VIEW_LOCK

# 主体在 cell 里占的高度比例（留头顶与脚边余量，避免贴边被引擎切到）
_FILL = 0.86


def frame_suffix(clip: str, idx: int, total: int, dir_id: str,
                 view_phrase: str = "") -> str:
    """拼在提示词尾部的动画修饰——姿态 + 朝向 + 一致性要求。

    view_phrase：身份卡里没有这个朝向时补的视角词（如「躯干转 45° 背对」），
    用来让图生图在参考栏的基础上把角度掰过去。
    """
    poses = CLIPS.get(clip, {}).get("poses") or [""]
    pose = poses[min(idx, len(poses) - 1)]
    facing = view_phrase or DIRECTIONS.get(dir_id, {}).get("prompt", "")
    return (f"animation frame {idx + 1} of {total}: {pose}, {facing}, "
            "same character as the reference, identical costume, hairstyle and colors, "
            "identical proportions, identical camera angle, "
            "feet planted on the same ground line")


def key_suffix(clip: str, idx: int, total: int, dir_id: str,
               view_phrase: str = "") -> str:
    """关键姿态的提示词尾部：比逐帧版更强调「照参考图这个人改姿势，别换人」。"""
    poses = CLIPS.get(clip, {}).get("poses") or [""]
    pose = poses[min(idx, len(poses) - 1)]
    facing = view_phrase or DIRECTIONS.get(dir_id, {}).get("prompt", "")
    return (f"key pose of the {CLIPS.get(clip, {}).get('name', clip)} cycle, "
            f"frame {idx + 1} of {total}: {pose}, {facing}, "
            "keep the exact same character as the reference sheet column: "
            "same face, same hairstyle, same costume pieces, same colors, same proportions, "
            "same camera tilt, full body, feet on the ground line")


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
    for clip in CLIP_ORDER:
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


def tween_cell(im: Image.Image, dy: int) -> Image.Image:
    """补帧：把整帧在 cell 内垂直平移 dy 像素（正=下沉，负=抬高）。

    只还原「身体起伏」这一维——传统二维步态里，接触帧压低、过渡帧抬高，
    中间那四帧本来就不是新画的一张，而是同一张的高低差。所以不做真正的
    光学流插值（两个姿态叠化会长出两条腿），只做整帧位移，不产生重影。
    """
    dy = int(round(dy))
    if not dy:
        return im.copy()
    w, h = im.size
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if dy > 0:                      # 下沉：底部 dy 行被裁掉（脚底下方留白够用）
        out.paste(im.crop((0, 0, w, h - dy)), (0, dy))
    else:                           # 抬高：顶部 -dy 行被裁掉（头顶也留了余量）
        out.paste(im.crop((0, -dy, w, h)), (0, 0))
    return out


def build_frames_from_keys(keys: dict[str, dict[int, Path]],
                           tweens: dict[str, dict[int, tuple[int, int]]],
                           counts: dict[str, int],
                           cell: tuple[int, int],
                           anchor: tuple[int, int]) -> dict[str, list[Image.Image]]:
    """关键姿态（少量真出的图）+ 插值帧表 → 完整 cell 帧序列。

    keys  = {clip: {帧号: 图路径}}，只有关键帧；
    tweens= {clip: {帧号: (源关键帧号, dy)}}，其余帧靠源关键帧垂直位移补。
    缩放按该向第一张关键帧统一（逐帧各算会让角色走动时一胀一缩），
    与 12.x 的 build_frames 保持同一标尺口径。
    """
    scale = None
    for clip in CLIP_ORDER:
        kd = keys.get(clip) or {}
        for i in sorted(kd):
            im = Image.open(kd[i]).convert("RGBA")
            if im.getbbox():
                scale = _fit_scale(im, cell)
            break
        if scale:
            break
    scale = scale or 1.0

    out: dict[str, list[Image.Image]] = {}
    for clip in CLIP_ORDER:
        kd = keys.get(clip) or {}
        if not kd:
            continue
        placed = {i: place(Image.open(p), cell, anchor, scale) for i, p in kd.items()}
        n = int(counts.get(clip) or max(kd) + 1)
        tw = tweens.get(clip) or {}
        frames: list[Image.Image] = []
        for i in range(n):
            if i in placed:
                frames.append(placed[i])
                continue
            src, dy = tw.get(i, (max(placed), 0))
            base = placed.get(src)
            if base is None:                      # 源关键帧缺失 → 退到最近的关键帧
                src = min(placed, key=lambda k: abs(k - i))
                base = placed[src]
            frames.append(tween_cell(base, dy))
        out[clip] = frames
    return out


# ------------------------------------------------------------
#  身份卡切栏（13.0）：一张多栏身份卡 → 每个朝向单独一张参考图。
#  为什么要切：图生图（融合编辑）一次吃多张参考图时，模型会把「整张三栏」
#  当成一张画去理解，容易把三栏的关系也画出来；切成单栏喂进去，参考图里
#  只有一个角色、一个朝向，指向性最强。
# ------------------------------------------------------------
def crop_column(sheet: Path, cols: int, col: int, dst: Path) -> Path:
    """从多栏身份卡里切出第 col 栏（cols 等分）。"""
    im = Image.open(sheet).convert("RGB")
    cw = im.width / max(1, cols)
    box = (int(round(col * cw)), 0, int(round((col + 1) * cw)), im.height)
    dst.parent.mkdir(parents=True, exist_ok=True)
    im.crop(box).save(dst)
    return dst


def save_bank(frames: dict[str, list[Image.Image]], dst: Path,
              cell: tuple[int, int]) -> dict:
    """落一张图集 PNG（每段一行：站立 / 行走 / 奔跑），返回元数据。"""
    rows = [(clip, frames[clip]) for clip in CLIP_ORDER if frames.get(clip)]
    sheet, layout = compose_sheet(rows, cell)
    dst.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(dst)
    return {"file": dst.name, "size": list(sheet.size), **layout}


# ------------------------------------------------------------
#  经典整表（11.1）：对标大话西游走路表的单张形态——
#  一行一个朝向（下→右下→右→右上→上→左上→左→左下）、一列一步态帧，
#  透明底。各方向图集里把行走行 cell 切回来重排；档位没实拍的镜像向
#  （w/nw/sw）从源向水平翻转填入，与运行时镜像像素级等价。
# ------------------------------------------------------------
CLASSIC_ORDER = ["s", "se", "e", "ne", "n", "nw", "w", "sw"]


def classic_sheet(am: dict, out_dir: Path, stem: str,
                  clip: str = "walk") -> tuple[Path | None, dict]:
    """把动画记录（anim 元数据）重排成经典整表。clip = walk | run（站立帧少，不给整表）。

    返回 (png 路径或 None, 元数据)。
    """
    if clip not in ("walk", "run"):
        clip = "walk"
    cw, ch = (list(am.get("cell") or VIEW_LOCK["cell"]) + [192, 192])[:2]
    banks, mirror = am.get("banks") or {}, am.get("mirror") or {}
    count = int(((am.get("clips") or {}).get(clip) or {}).get("count") or 0)
    suffix = "" if clip == "walk" else f"_{clip}"
    if not banks or not count:
        return None, {"reason": f"no {clip} frames", "clip": clip}
    rows, used = [], []
    for d in CLASSIC_ORDER:
        src = d if d in banks else mirror.get(d)
        bank = banks.get(src or "")
        if not bank:
            continue
        bp = Path(bank.get("path") or "")
        if not bp.is_absolute():
            bp = Path(".") / bp
        if not bp.exists():
            continue
        row = ((bank.get("clips") or {}).get(clip) or {}).get("row")
        if row is None:
            continue
        sheet = Image.open(bp).convert("RGBA")
        cols = max(1, sheet.width // cw)
        cells = []
        for c in range(count):
            cc = c % cols                       # 该向列数不足时循环补位
            im = sheet.crop((cc * cw, row * ch, (cc + 1) * cw, (row + 1) * ch))
            if src != d:
                im = im.transpose(Image.FLIP_LEFT_RIGHT)
            cells.append(im)
        rows.append(cells)
        used.append({"dir": d, "from": src, "mirrored": src != d})
    if not rows:
        return None, {"reason": "no bank files", "clip": clip}
    cols = max(len(cs) for cs in rows)
    out = Image.new("RGBA", (cw * cols, ch * len(rows)), (0, 0, 0, 0))
    for r, cells in enumerate(rows):
        for c, im in enumerate(cells):
            out.alpha_composite(im, (c * cw, r * ch))
    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / f"{stem}_classic{suffix}.png"
    out.save(png)
    return png, {"clip": clip, "rows": len(rows), "cols": cols, "cell": [cw, ch],
                 "dirs": [u["dir"] for u in used], "used": used}


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
