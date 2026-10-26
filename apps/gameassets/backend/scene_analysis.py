# -*- coding: utf-8 -*-
"""场景语义分析（9.0）：从一张场景概念图里识别「哪里能走、哪里不能走」。

游戏里场景图从来不是一张画——拆包看任何一款 2D 游戏，场景旁边必然跟着
碰撞层（collision layer / walkability grid）。我们的场景是模型生成的概念图，
没有现成碰撞层，就自己算一层出来：

    地平线以上            → 天空，不可走
    地平线以下            → 地面候选
      与地面主色接近 & 局部纹理平坦 → 可行走
      强边缘/竖直结构（树、墙、崖）→ 障碍
      近黑区域              → 阴影/洞口，不可走

产出三样东西，全部挂到场景素材记录的 walk 字段：
    mask      可行走掩码 PNG（1/4 分辨率灰度图，255=可走）
    overlay   绿/红叠加预览（人眼一眼复核对不对）
    meta      地平线、可行走占比、战斗带、出生点、主要障碍框

这是启发式 v1：对「地面在下半部、天空/远景在上半部」的横版概念图足够准；
等距/俯视图后续接入时再按用途分派算法。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from shared.config import settings

_MASK_W = 256                     # 掩码工作分辨率（宽），高按场景比例
_GROUND_BAND = 0.14               # 底部 14% 行当「地面主色」采样带
_COLOR_TOL = 46.0                 # 与地面主色的色距阈值（0-441）
_EDGE_TOL = 26.0                  # 局部梯度均值阈值（纹理平坦才算路面）
_DARK_LUM = 42.0                  # 近黑视为阴影/洞口


def _load_rgb(path: Path) -> Image.Image:
    return Image.open(path).convert("RGB")


def _find_horizon(arr: np.ndarray) -> int:
    """地平线：自上而下找第一行「多数像素明显偏离天空参考色」的行。
    天空参考色 = 顶部 8% 行的中位数。找不到就退回 40% 高度。
    注意云海/雾景会让「颜色偏离天空」提前触发，调用方须按视角模式钳制下限。"""
    h, w, _ = arr.shape
    sky = np.median(arr[: max(2, int(h * 0.08))].reshape(-1, 3), axis=0)
    dist = np.sqrt(((arr.astype("float32") - sky) ** 2).sum(axis=2))
    row_frac = (dist > 60).mean(axis=1)
    for y in range(int(h * 0.15), int(h * 0.9)):
        if row_frac[y] > 0.55 and row_frac[min(y + 3, h - 1)] > 0.55:
            return y
    return int(h * 0.40)


def _edge_map(arr: np.ndarray) -> np.ndarray:
    """灰度梯度幅值（Sobel 近似，numpy 实现）。"""
    g = arr.mean(axis=2)
    gx = np.zeros_like(g); gy = np.zeros_like(g)
    gx[:, 1:-1] = np.abs(g[:, 2:] - g[:, :-2]) * 0.5
    gy[1:-1, :] = np.abs(g[2:, :] - g[:-2, :]) * 0.5
    # 3×3 均值模糊，把单像素噪声抹平
    k = np.ones((3, 3)) / 9.0
    from scipy.ndimage import convolve
    return convolve(np.sqrt(gx ** 2 + gy ** 2), k, mode="nearest")


def _clean(mask: np.ndarray) -> np.ndarray:
    """形态学清理：闭运算填小洞 → 开运算去毛刺 → 只留最大连通域，
    并保留占地面 3% 以上的次大块（多片草地/多段路面）。"""
    from scipy.ndimage import (binary_closing, binary_opening, label,
                               find_objects)
    mask = binary_closing(mask, np.ones((3, 3)))
    mask = binary_opening(mask, np.ones((3, 3)))
    lab, n = label(mask)
    if n == 0:
        return mask
    sizes = np.bincount(lab.ravel())
    sizes[0] = 0
    keep = set(np.argsort(sizes)[::-1][:1].tolist())      # 最大块必留
    ground_area = int(mask.sum())
    for i in range(1, n + 1):
        if i in keep:
            continue
        if sizes[i] >= ground_area * 0.03:
            keep.add(i)
    out = np.isin(lab, list(keep))
    return out


def _blocked_blobs(mask: np.ndarray, horizon: int) -> list[dict]:
    """主要障碍块（地面上的不可走连通域，按面积取前 5），相对坐标框。"""
    from scipy.ndimage import label
    blocked = ~mask
    blocked[:horizon, :] = False
    lab, n = label(blocked)
    if n == 0:
        return []
    sizes = np.bincount(lab.ravel())
    sizes[0] = 0
    h, w = mask.shape
    out = []
    for i in np.argsort(sizes)[::-1][:5]:
        if sizes[i] < mask.size * 0.004:             # 太小的不算障碍
            continue
        ys, xs = np.where(lab == i)
        out.append({"box": [round(xs.min() / w, 3), round(ys.min() / h, 3),
                            round(xs.max() / w, 3), round(ys.max() / h, 3)],
                    "area": round(float(sizes[i]) / mask.size, 3)})
    return out


def _combat_band(mask: np.ndarray, horizon: int) -> list[float] | None:
    """战斗带：可行走覆盖率 >40% 的连续行区间（归一化）——角色活动的主地带。
    概念图的路面常被树影/花纹打断，0.55 会一无所获，0.30 是实测下限。"""
    h = mask.shape[0]
    frac = mask[horizon:].mean(axis=1)
    rows = np.where(frac > 0.30)[0]
    if len(rows) == 0:
        return None
    # 取最长连续段
    splits = np.split(rows, np.where(np.diff(rows) > 6)[0] + 1)
    seg = max(splits, key=len)
    if len(seg) < h * 0.04:                          # 太薄不成带
        return None
    return [round(float(horizon + seg[0]) / h, 3),
            round(float(horizon + seg[-1]) / h, 3)]


def _spawns(mask: np.ndarray, band: list[float] | None) -> list[list[float]]:
    """出生点：战斗带内左/中/右三个可走点（找不到带就退而求其次全图找）。"""
    h, w = mask.shape
    if band:
        y0, y1 = int(band[0] * h), int(band[1] * h)
    else:
        y0, y1 = int(h * 0.6), int(h * 0.95)
    region = mask[y0:y1]
    pts = []
    for fx in (0.22, 0.5, 0.78):
        col = region[:, int(fx * w)]
        rows = np.where(col)[0]
        if len(rows):
            y = y0 + int(rows.mean())
            pts.append([round(float(fx), 3), round(float(y) / h, 3)])
    if not pts:                                      # 兜底：全图任意可走点
        ys, xs = np.where(mask)
        if len(ys):
            pts.append([round(float(xs.mean()) / w, 3),
                        round(float(ys.mean()) / h, 3)])
    return pts


def analyze(scene_path: str | Path, out_dir: Path,
            mode: str = "sideview", horizon_min: float = 0.28) -> dict:
    """场景图 → walk mask + overlay + meta。纯 CPU，单张 <1s。

    mode: sideview（横版/竖屏，天空不可走）| topdown（平铺/等距，全域只抠障碍）。
    sideview 下地平线不低于 horizon_min——云海/雾景的颜色检测会误报过早，
    而 2.5D 游戏的可行走区本来就在画面下部（拆包看碰撞层都在底部 1/3）。
    """
    img = _load_rgb(scene_path)
    w0, h0 = img.size
    w = _MASK_W
    h = max(64, round(w * h0 / w0))
    arr = np.asarray(img.resize((w, h), Image.LANCZOS)).astype("float32")

    if mode == "topdown":
        horizon = 0
        ground_ref = np.median(arr.reshape(-1, 3), axis=0)
    else:
        horizon = max(_find_horizon(arr), int(h * horizon_min))
        ground_ref = np.median(
            arr[int(h * (1 - _GROUND_BAND)):].reshape(-1, 3), axis=0)

    dist = np.sqrt(((arr - ground_ref) ** 2).sum(axis=2))
    edge = _edge_map(arr)
    lum = arr.mean(axis=2)

    walk = (dist < _COLOR_TOL) & (edge < _EDGE_TOL) & (lum > _DARK_LUM)
    if mode != "topdown":
        walk[:horizon, :] = False                    # 天空/远景不可走
    walk = _clean(walk)

    band = _combat_band(walk, horizon)
    spawns = _spawns(walk, band)
    obstacles = _blocked_blobs(walk, horizon)

    out_dir.mkdir(parents=True, exist_ok=True)
    mask_img = Image.fromarray((walk * 255).astype("uint8"), "L")
    mask_path = out_dir / f"{Path(scene_path).stem}_walk.png"
    mask_img.save(mask_path)

    # 叠加预览：可走=绿、障碍=红（半透明），人眼一眼复核
    ov = img.resize((w, h), Image.LANCZOS).convert("RGBA")
    tint = np.zeros((h, w, 4), dtype="uint8")
    tint[walk] = (60, 220, 90, 90)
    blocked = (~walk)
    blocked[:horizon, :] = False
    tint[blocked] = (230, 60, 50, 90)
    ov = Image.alpha_composite(ov, Image.fromarray(tint, "RGBA"))
    d = ImageDraw.Draw(ov)
    dy = int(horizon)
    d.line([(0, dy), (w, dy)], fill=(255, 210, 60, 255), width=2)
    if band:
        d.line([(0, int(band[0] * h)), (w, int(band[0] * h))],
               fill=(80, 160, 255, 200), width=1)
        d.line([(0, int(band[1] * h)), (w, int(band[1] * h))],
               fill=(80, 160, 255, 200), width=1)
    for p in spawns:
        cx, cy = int(p[0] * w), int(p[1] * h)
        d.ellipse((cx - 4, cy - 4, cx + 4, cy + 4), outline=(255, 255, 255, 255),
                  width=2)
    overlay_path = out_dir / f"{Path(scene_path).stem}_walkov.png"
    ov.convert("RGB").save(overlay_path, quality=88)

    try:
        rel = mask_path.resolve().relative_to(settings.data_dir_path.resolve())
        mask_url = f"/data/{rel.as_posix()}"
        rel2 = overlay_path.resolve().relative_to(settings.data_dir_path.resolve())
        overlay_url = f"/data/{rel2.as_posix()}"
    except ValueError:
        mask_url, overlay_url = f"/data/{mask_path.name}", f"/data/{overlay_path.name}"

    return {
        "mask_path": str(mask_path), "mask_url": mask_url,
        "overlay_url": overlay_url,
        "mask_size": [w, h], "scene_size": [w0, h0],
        "mode": mode,
        "horizon": round(horizon / h, 3),
        "walk_ratio": round(float(walk[horizon:].mean()), 3) if horizon < h else 0.0,
        "combat_band": band,
        "spawns": spawns,
        "obstacles": obstacles,
        "method": "v1-heuristic",
    }


# 用途 → 分析模式：平铺/等距是俯视逻辑（全域抠障碍），
# 其余是横版逻辑（下半部走路，上面全是天/远景）。
def _mode_of(usage: str | None) -> tuple[str, float]:
    if usage in ("tile", "iso"):
        return "topdown", 0.0
    return "sideview", {"banner": 0.35, "portrait": 0.25}.get(usage or "", 0.28)


def analyze_asset(scene_asset: dict, force: bool = False) -> dict:
    """按素材路径分析，结果按 (path, mtime, mode) 缓存在 walk/ 目录。"""
    p = Path(scene_asset["path"])
    if not p.is_file():
        raise FileNotFoundError(p)
    mode, horizon_min = _mode_of(scene_asset.get("usage"))
    out_dir = settings.data_dir_path / "outputs" / "gameassets" / "walk"
    stamp = out_dir / f"{p.stem}_walk.json"
    key = f"{p}:{int(p.stat().st_mtime)}:{mode}:{horizon_min}"
    import json
    if stamp.is_file() and not force:
        try:
            data = json.loads(stamp.read_text(encoding="utf-8"))
            if data.get("_key") == key:
                return data
        except Exception:
            pass
    rep = analyze(p, out_dir, mode=mode, horizon_min=horizon_min)
    rep["_key"] = key
    stamp.write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
    return rep
