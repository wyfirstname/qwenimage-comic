# -*- coding: utf-8 -*-
"""游戏素材工坊自测：tonecheck 单元测试（不依赖引擎/网络）。

用例：
    1. 主色板提取：纯色图返回该色；渐变图返回多个色且占比降序
    2. 同色调两张图 → pass；冷暖对撞图 → 不 pass
    3. 距离分数单调性：偏移越大分越高
    4. 空色板兜底 = 255
"""
import io
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, r"E:\aigc\qwenimages")

from apps.gameassets.backend import tonecheck  # noqa: E402


def img_bytes(draw_fn) -> bytes:
    img = Image.new("RGB", (128, 128))
    draw_fn(ImageDraw.Draw(img), img)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def flat(color):
    return img_bytes(lambda d, im: d.rectangle([0, 0, 128, 128], fill=color))


def main() -> int:
    failures = []

    # 1) 纯色提取
    pal = tonecheck.extract_palette(flat((217, 58, 53)))
    if not pal or pal[0]["hex"] != "#D93A35":
        failures.append(f"纯色提取错误：{pal[:2]}")

    # 2) 渐变图多色、占比降序
    def grad(d, im):
        for y in range(128):
            d.line([(0, y), (128, y)], fill=(40 + y, 80, 200 - y))
    pal2 = tonecheck.extract_palette(grad.__call__ and img_bytes(grad))
    if len(pal2) < 3:
        failures.append(f"渐变图主色应 >= 3，实际 {len(pal2)}")
    shares = [c["share"] for c in pal2]
    if shares != sorted(shares, reverse=True):
        failures.append(f"主色未按占比降序：{shares}")

    # 3) 同色调 vs 异色调
    #    dusk_a/dusk_b 是「同一黄昏氛围的两次出图」：色相相同、亮度微移（色距 ~25）
    dusk_a = img_bytes(lambda d, im: (
        d.rectangle([0, 0, 128, 64], fill=(240, 131, 0)),
        d.rectangle([0, 64, 128, 128], fill=(232, 90, 123))))
    dusk_b = img_bytes(lambda d, im: (
        d.rectangle([0, 0, 128, 64], fill=(232, 120, 20)),
        d.rectangle([0, 64, 128, 128], fill=(210, 80, 110))))
    night_img = img_bytes(lambda d, im: (
        d.rectangle([0, 0, 128, 64], fill=(31, 46, 92)),
        d.rectangle([0, 64, 128, 128], fill=(20, 20, 30))))

    ok = tonecheck.check(dusk_b, dusk_a)
    bad = tonecheck.check(night_img, dusk_a)
    if not ok["pass"]:
        failures.append(f"黄昏近似图应 pass，实际 score={ok['score']} 阈值={ok['threshold']}")
    if bad["pass"]:
        failures.append(f"夜景 vs 黄昏不应 pass，实际 score={bad['score']}")
    if not (bad["score"] > ok["score"]):
        failures.append(f"分数应单调：bad={bad['score']} ok={ok['score']}")

    # 4) 空色板兜底
    d = tonecheck.palette_distance([], [])
    if d != 255.0:
        failures.append(f"空色板距离应为 255，实际 {d}")

    print(f"近似: score={ok['score']} pass={ok['pass']} | "
          f"对撞: score={bad['score']} pass={bad['pass']}")
    if failures:
        print("FAIL:")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL PASS")
    return 0


def main_v5():
    """5.0 新模块自测：quality 守门 / transmat 抠图 / exporter 元数据。"""
    failures = []
    from apps.gameassets.backend import quality, transmat
    from apps.gameassets.backend.presets import ASPECTS, asset_size, transparent_mode

    black = img_bytes(lambda d, im: d.rectangle([0, 0, 128, 128], fill=(2, 4, 10)))
    noise = img_bytes(lambda d, im: [
        d.point((x, y), (x * 2 % 256, y * 2 % 256, (x + y) % 256))
        for x in range(0, 128, 4) for y in range(0, 128, 4)] and None)
    # 1) 黑图三重命中
    q = quality.inspect(black, None)
    if q["pass"] or len(q["reasons"]) < 2:
        failures.append(f"黑图应不通过且多理由：{q}")
    # 2) 正常结构图（tone 无参照）应通过结构检测
    q2 = quality.inspect(noise, None)
    if not q2["metrics"]["entropy"] > quality.ENTROPY_MIN:
        failures.append(f"噪声图熵应达标：{q2['metrics']}")
    # 3) 色键抠图：纯白图 → 全透明；纯红图 → 不透明
    import tempfile, os
    tmp = tempfile.mkdtemp()
    src_w = os.path.join(tmp, "w.png")
    Image.open(io.BytesIO(flat((255, 255, 255)))).save(src_w)
    dst_w = os.path.join(tmp, "w_a.png")
    rep = transmat.make_transparent(src_w, dst_w, "chroma")
    if rep["alpha_ratio"] > 0.02:
        failures.append(f"纯白图应几乎全透明：{rep}")
    src_r = os.path.join(tmp, "r.png")
    Image.open(io.BytesIO(flat((200, 30, 30)))).save(src_r)
    rep2 = transmat.make_transparent(src_r, os.path.join(tmp, "r_a.png"), "chroma")
    if rep2["alpha_ratio"] < 0.98:
        failures.append(f"纯色前景应几乎不透明：{rep2}")
    # 4) 画幅对齐 64
    for kind, (w, h) in ASPECTS.items():
        aw, ah = asset_size({}, kind)
        if aw % 64 or ah % 64:
            failures.append(f"{kind} 画幅未 64 对齐：{aw}x{ah}")
    # 5) 透明底默认档
    if transparent_mode({}, "character") != "chroma" or transparent_mode({}, "scene") != "none":
        failures.append("透明底默认档不对")

    if failures:
        print("FAIL:")
        for f in failures:
            print("  -", f)
        return 1
    print("V5 ALL PASS")
    return 0


if __name__ == "__main__":
    rc = main()
    rc2 = main_v5()
    sys.exit(rc or rc2)
