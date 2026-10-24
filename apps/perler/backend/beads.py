# -*- coding: utf-8 -*-
"""拼豆转换核心：图片 → 豆板网格。

纯 Pillow 实现，无 GPU 依赖。流程：
    1. 透明底合成到白底（拼豆没有透明豆）；
    2. LANCZOS 缩到目标豆数（先缩后量化，避免逐豆取样丢失整体轮廓）；
    3. 每豆映射到色板最近色（欧氏距离，sRGB 直取——豆色本来就离散，够用）；
    4. 可选 Floyd-Steinberg 抖动：把量化误差扩散给右/下邻豆，
       大面积渐变（天空、皮肤）观感明显更好，图标类小图建议关。

输出 grid 是 行优先 的色板索引二维表，直接可画、可数、可导出。
"""

from __future__ import annotations

import io

from PIL import Image

from .palettes import get_palette

MAX_WIDTH = 200    # 豆板宽度上限（防呆：再宽就数不清了）
MAX_HEIGHT = 400


def _flatten_rgb(img: Image.Image) -> Image.Image:
    """透明底合成白底，转 RGB。"""
    if img.mode == "RGBA":
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    return img.convert("RGB")


def _nearest(color: tuple[int, int, int], palette_rgb: list[tuple[int, int, int]]) -> int:
    """最近色索引。线性扫 —— 色板 ≤ 256 色、网格 ≤ 8 万豆，纯 Python 毫秒级到亚秒级。"""
    r, g, b = color
    best, best_d = 0, 1 << 30
    for i, (pr, pg, pb) in enumerate(palette_rgb):
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < best_d:
            best, best_d = i, d
            if d == 0:
                break
    return best


def convert_image(
    data: bytes,
    width: int,
    palette_id: str,
    dither: bool = False,
) -> dict:
    """把图片转成拼豆网格。

    返回：
        grid      行优先索引表 grid[y][x]，值 = 色板 colors 的下标
        width/height  豆板尺寸（豆）
        colors    用到的颜色 [{index, id, name, hex, count}]（按数量降序）
        total     总豆数
        preview   缩略预览 PNG 的原始字节（每豆 1 像素 + NEAREST 放大由前端做）
    """
    palette = get_palette(palette_id)
    colors = palette["colors"]
    palette_rgb = [(int(c[2][1:3], 16), int(c[2][3:5], 16), int(c[2][5:7], 16))
                   for c in colors]

    width = max(4, min(int(width), MAX_WIDTH))

    img = _flatten_rgb(Image.open(Image.io.BytesIO(data)))
    w, h = img.size
    if w == 0 or h == 0:
        raise ValueError("图片尺寸无效")
    height = max(1, min(round(h / w * width), MAX_HEIGHT))

    small = img.resize((width, height), Image.LANCZOS)
    px = small.load()

    grid: list[list[int]] = [[0] * width for _ in range(height)]
    if dither:
        _floyd_steinberg(px, width, height, palette_rgb, grid)
    else:
        for y in range(height):
            row = grid[y]
            for x in range(width):
                row[x] = _nearest(px[x, y], palette_rgb)

    # 逐色计数（只出现在图里的色才列出，采购清单更干净）
    counts: dict[int, int] = {}
    for row in grid:
        for idx in row:
            counts[idx] = counts.get(idx, 0) + 1

    color_stats = [
        {
            "index": i,
            "id": colors[i][0],
            "name": colors[i][1],
            "hex": colors[i][2],
            "count": counts[i],
        }
        for i in sorted(counts, key=lambda k: -counts[k])
    ]

    # 1:1 像素预览（前端 NEAREST 放大，每豆方方正正）
    preview_img = Image.new("RGB", (width, height))
    preview_img.putdata(
        [tuple(palette_rgb[grid[y][x]]) for y in range(height) for x in range(width)]
    )
    buf = io.BytesIO()
    preview_img.save(buf, format="PNG")

    return {
        "grid": grid,
        "width": width,
        "height": height,
        "colors": color_stats,
        "total": width * height,
        "palette_name": palette["name"],
        "preview": buf.getvalue(),
    }


def _floyd_steinberg(px, width: int, height: int,
                     palette_rgb: list[tuple[int, int, int]],
                     grid: list[list[int]]) -> None:
    """原地 Floyd-Steinberg：量化误差按 7/3、5、1 权重扩散到右、下、左下、右下。"""
    # 浮点工作区，避免误差被截断成整数抖不动
    work = [[list(px[x, y]) for x in range(width)] for y in range(height)]
    for y in range(height):
        for x in range(width):
            old = work[y][x]
            quant = tuple(max(0, min(255, round(c))) for c in old)
            idx = _nearest(quant, palette_rgb)
            grid[y][x] = idx
            err = tuple(oc - qc for oc, qc in zip(old, quant))
            for dx, dy, wgt in ((1, 0, 7), (-1, 1, 3), (0, 1, 5), (1, 1, 1)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < width and 0 <= ny < height:
                    cell = work[ny][nx]
                    for c in range(3):
                        cell[c] += err[c] * wgt / 16.0


def render_sheet_png(
    grid: list[list[int]],
    colors: list[dict],
    palette_colors: list[list],
    cell: int = 26,
) -> bytes:
    """渲染可打印图纸：网格线 + 每 5 格坐标 + 右侧图例（颜色/名称/数量）。"""
    from PIL import ImageDraw, ImageFont

    h, w = len(grid), len(grid[0])
    margin = cell                       # 四周留坐标带
    legend_w = 190
    img = Image.new("RGB", (margin + w * cell + legend_w,
                            margin + h * cell + margin), "#FFFFFF")
    draw = ImageDraw.Draw(img)
    font = _cjk_font(13)

    rgb = [(int(c[2][1:3], 16), int(c[2][3:5], 16), int(c[2][5:7], 16))
           for c in palette_colors]

    # 豆格
    for y in range(h):
        for x in range(w):
            x0, y0 = margin + x * cell, margin + y * cell
            draw.rectangle([x0, y0, x0 + cell - 1, y0 + cell - 1],
                           fill=rgb[grid[y][x]])
    # 网格线（细线浅灰，每 5 格加粗深灰，方便对着数）
    light, dark = (210, 210, 210), (90, 90, 90)
    for x in range(w + 1):
        gx = margin + x * cell
        draw.line([(gx, margin), (gx, margin + h * cell)],
                  fill=dark if x % 5 == 0 else light, width=1)
    for y in range(h + 1):
        gy = margin + y * cell
        draw.line([(margin, gy), (margin + w * cell, gy)],
                  fill=dark if y % 5 == 0 else light, width=1)

    # 坐标标注
    for x in range(0, w, 5):
        draw.text((margin + x * cell + 2, 2), str(x + 1), fill="black", font=font)
    for y in range(0, h, 5):
        draw.text((2, margin + y * cell + 2), str(y + 1), fill="black", font=font)

    # 图例
    lx = margin + w * cell + 8
    draw.text((lx, margin), "配色清单", fill="black", font=font)
    ly = margin + 20
    sw = 14
    for c in colors[:60]:  # 图例最多列 60 行，超出按数量截断
        if ly + sw > img.height - margin:
            break
        draw.rectangle([lx, ly, lx + sw, ly + sw], fill=c["hex"],
                       outline=(120, 120, 120))
        draw.text((lx + sw + 5, ly + 1),
                  f"{c['name']} ×{c['count']}", fill="black", font=font)
        ly += sw + 5

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# 中文字体：Windows 优先微软雅黑/黑体，Linux 常见 Noto/文泉驿；都没有再退回默认字体
_CJK_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)
_font_cache: dict[int, object] = {}


def _cjk_font(size: int = 13):
    if size in _font_cache:
        return _font_cache[size]
    from PIL import ImageFont
    font = None
    for path in _CJK_FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(path, size)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
        print("[perler] 未找到系统中文字体，图纸图例的中文将显示为方框")
    _font_cache[size] = font
    return font
