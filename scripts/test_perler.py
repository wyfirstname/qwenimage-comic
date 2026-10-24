# -*- coding: utf-8 -*-
"""拼豆核心逻辑自测：直接调 beads.convert_image，不经过网络。

用例：
    1. 生成一张含渐变+色块的测试图（24x24 放大模拟照片感）
    2. 不开抖动 / 开抖动 各转一次
    3. 校验：网格尺寸、总豆数闭合（计数之和 == 宽×高）、
       颜色索引全部落在色板范围内、逐色统计与直接数网格一致
    4. 校验图纸 PNG 能渲染出来且尺寸合理
"""
import io
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, r"E:\aigc\qwenimages")

from apps.perler.backend import beads  # noqa: E402
from apps.perler.backend.palettes import get_palette  # noqa: E402


def make_test_image() -> bytes:
    """左半红右半蓝 + 顶部黄条 + 对角渐变，覆盖色块与渐变两种场景。"""
    img = Image.new("RGB", (256, 256))
    d = ImageDraw.Draw(img)
    for y in range(256):
        for x in range(256):
            if y < 48:
                c = (247, 211, 30)
            elif x < 128:
                c = (217, 58, 53)
            else:
                t = (y - 48) / 208
                c = (int(40 + t * 60), int(80 + t * 40), int(200 + t * 40))
            img.putpixel((x, y), c)
    d.ellipse([64, 96, 192, 224], fill=(32, 32, 32))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main() -> int:
    data = make_test_image()
    failures = []

    for dither in (False, True):
        r = beads.convert_image(data, width=58, palette_id="common48", dither=dither)
        W, H = r["width"], r["height"]
        tag = "dither" if dither else "plain"

        # 1) 尺寸：宽 58，高按 1:1 比例应约 58
        if W != 58:
            failures.append(f"[{tag}] 宽度应为 58，实际 {W}")
        if not (50 <= H <= 66):
            failures.append(f"[{tag}] 高度异常：{H}（应为 58 上下）")

        # 2) 总豆闭合：计数之和 == W*H == 网格豆数
        counted = sum(c["count"] for c in r["colors"])
        if counted != r["total"] or counted != W * H:
            failures.append(f"[{tag}] 计数不闭合：sum={counted} total={r['total']} W*H={W*H}")

        # 3) 索引范围 + 逐格重数一致
        n_colors = 48
        grid_count: dict[int, int] = {}
        for row in r["grid"]:
            if len(row) != W:
                failures.append(f"[{tag}] 行长不等于 W")
                break
            for idx in row:
                if not (0 <= idx < n_colors):
                    failures.append(f"[{tag}] 索引越界：{idx}")
                    break
                grid_count[idx] = grid_count.get(idx, 0) + 1
        stat_map = {c["index"]: c["count"] for c in r["colors"]}
        if grid_count != stat_map:
            failures.append(f"[{tag}] 逐色统计与网格重数不一致")
        if len(r["colors"]) != len(grid_count):
            failures.append(f"[{tag}] colors 列表长度 {len(r['colors'])} != 实际用色 {len(grid_count)}")

        # 4) 颜色降序
        cnts = [c["count"] for c in r["colors"]]
        if cnts != sorted(cnts, reverse=True):
            failures.append(f"[{tag}] colors 未按数量降序")

        # 5) 预览图可解析、尺寸对
        pv = Image.open(io.BytesIO(r["preview"]))
        if pv.size != (W, H):
            failures.append(f"[{tag}] 预览尺寸 {pv.size} != ({W},{H})")

        print(f"[{tag}] {W}x{H} 总豆 {r['total']} 用色 {len(r['colors'])} "
              f"前3色: {[(c['name'], c['count']) for c in r['colors'][:3]]}")

    # 6) 图纸渲染
    r = beads.convert_image(data, width=29, palette_id="basic24", dither=False)
    sheet = beads.render_sheet_png(r["grid"], r["colors"],
                                   get_palette("basic24")["colors"])
    sim = Image.open(io.BytesIO(sheet))
    print(f"sheet: {sim.size[0]}x{sim.size[1]} PNG {len(sheet)//1024}KB")
    if sim.size[0] < 300 or sim.size[1] < 300:
        failures.append(f"图纸尺寸可疑：{sim.size}")

    # 7) 透明底：合成白底而非黑底（RGBA 图不留透明豆）
    rgba = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(rgba).ellipse([8, 8, 56, 56], fill=(217, 58, 53, 255))
    buf = io.BytesIO(); rgba.save(buf, format="PNG")
    r2 = beads.convert_image(buf.getvalue(), width=16, palette_id="basic24", dither=False)
    corners = {r2["grid"][0][0], r2["grid"][0][-1], r2["grid"][-1][0], r2["grid"][-1][-1]}
    white_idx = next(c["index"] for c in r2["colors"] if c["id"] == "white")
    if corners != {white_idx}:
        failures.append(f"透明底四角应为白色豆，实际索引 {corners}")

    if failures:
        print("\nFAIL:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
