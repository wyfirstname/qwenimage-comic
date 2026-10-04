# -*- coding: utf-8 -*-
"""回归测试：人像编辑（Qwen-Image 2.1 官方图像编辑玩法）。

覆盖：
  1. 玩法清单完整（8 个官方玩法、分组、编辑层级标签）
  2. 提示词组装符合官方结构（锁身份 / 三不变 / 不虚构 / 禁止项）
  3. 字段校验、参考图数量校验
  4. 多图引用 image_1…image_N 的编号正确
  5. 参考图像素预算归一化（总像素上限、最长边、64 倍数）
  6. 出图 payload 走多参考图编辑通道（mode=img2img + fusion=True），
     且不污染传统图生图 / 漫画分镜融合的口径
  7. 大字段外置（blobs）往返后参考图不丢

运行：backend> ..\\.venv\\Scripts\\python.exe tests\\test_portrait.py
不依赖模型与数据库；出图提交被拦截，只校验口径。
"""
import asyncio
import base64
import io
import os
import sys
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp(prefix="portrait_test_"))
os.environ["DB_PATH"] = str(tmp / "t.db")
os.environ["DATA_DIR"] = str(tmp / "data")
os.environ["OUTPUT_DIR"] = str(tmp / "data/outputs")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db as appdb  # noqa: E402

appdb.db

from PIL import Image  # noqa: E402

from app import blobs  # noqa: E402
from app.api import portrait as portrait_api  # noqa: E402
from app.schemas import PortraitComposeIn, PortraitSubmitIn  # noqa: E402
from app.services import generation, portrait  # noqa: E402
from app.services.portrait import PortraitError  # noqa: E402

fails = []


def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        fails.append(label)


def data_url(size=(640, 640), color=(180, 170, 160)):
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def noisy_data_url(size=(768, 768)):
    """随机噪点图：PNG 压不动，base64 一定超过 blobs 的外置阈值（4096 字符）。

    纯色图压缩后只有几 KB，测不出「大字段外置」这条链路。
    """
    img = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ============================================================
# 1. 玩法清单
# ============================================================
payload = portrait.presets_payload()
ids = [i["id"] for i in payload["items"]]
check("玩法数量 = 8", len(ids) == 8)
check("玩法 id 唯一", len(set(ids)) == len(ids))
check("含官方 4 类编辑玩法",
      {"portrait_scene", "portrait_hair", "portrait_expression", "photo_restore"} <= set(ids))
check("含风格化 / 多图合成 / 换装 / 文生图",
      {"photo_style", "multi_compose", "outfit_swap", "editorial_portrait"} <= set(ids))
check("分组包含图像编辑与文生图",
      payload["groups"] == ["图像编辑", "文生图"])
check("每个玩法都有层级标签",
      all(i["level_label"] for i in payload["items"]))
check("每个玩法都有字段定义",
      all(len(i["fields"]) >= 1 for i in payload["items"]))

multi = portrait.PRESET_MAP["multi_compose"]
check("多图合成需要 2~3 张", multi["images"] == {"min": 2, "max": 3})
check("换装需要 2 张", portrait.PRESET_MAP["outfit_swap"]["images"] == {"min": 2, "max": 2})
check("文生图玩法不需要参考图",
      portrait.PRESET_MAP["editorial_portrait"]["images"] == {"min": 0, "max": 0})

# ============================================================
# 2. 提示词结构（官方四条要点）
# ============================================================
scene = portrait.build_prompt("portrait_scene", {"scene": "雨夜天台，霓虹在积水里碎成一片"}, 1)
check("场景重绘：锁定身份语句存在", "沿用原图不变" in scene["prompt"])
check("场景重绘：包含用户输入的场景", "雨夜天台" in scene["prompt"])
check("场景重绘：包含光线统一逻辑", "受光与阴影遵循统一" in scene["prompt"])
check("场景重绘：含禁止项", "不出现任何可读文字" in scene["prompt"])
check("场景重绘：分为 5 段（锁身份/变更/光线/构图/画质）", len(scene["sections"]) == 5)
check("场景重绘：标记为语义级", scene["level"] == portrait.LEVEL_SEMANTIC)

hair = portrait.build_prompt("portrait_hair", {"hairstyle": "蓬松的大波浪长卷发"}, 1)
check("发型编辑：含三不变官方句式", "保持原图的取景、构图、缩放级别与人物在画面中的大小位置不变"
      in hair["prompt"])
check("发型编辑：含不重新构图声明", "不作重新构图或整体重绘" in hair["prompt"])
check("发型编辑：标记为外观级", hair["level"] == portrait.LEVEL_APPEARANCE)

expr = portrait.build_prompt("portrait_expression",
                             {"expression": "一同开口大笑", "action": "一起比耶"}, 1)
check("表情编辑：含手部结构约束", "手指数量" in expr["prompt"])
check("表情编辑：含光线顺应声明", "遵循原图统一的自然光照逻辑" in expr["prompt"])

restore = portrait.build_prompt("photo_restore", {}, 1)
check("老照片修复：含 not fabricate / do not beautify",
      "rather than inventing" in restore["prompt"] and "beautify" in restore["prompt"])
check("老照片修复：保留原始比例与取景", "Strictly preserve the original aspect ratio" in restore["prompt"])

style = portrait.build_prompt("photo_style", {"style": "印象派油画"}, 1)
check("风格化：禁止新增文字（默认 allow_text=no）", "不新增原图没有的文字" in style["prompt"])
style_ok = portrait.build_prompt("photo_style",
                                 {"style": "印象派油画", "allow_text": "yes"}, 1)
check("风格化：允许保留原图文字时口径切换", "保留其位置与内容" in style_ok["prompt"])

compose2 = portrait.build_prompt("multi_compose", {"compose": "让两人并肩而立"}, 2)
check("多图合成：引用 image_2", "image_2" in compose2["prompt"])
check("多图合成：含禁止拼贴", "禁止拼贴" in compose2["prompt"])
compose3 = portrait.build_prompt("multi_compose", {"compose": "三人合影"}, 3)
check("多图合成：三图时引用 image_3", "image_3" in compose3["prompt"])

outfit = portrait.build_prompt("outfit_swap", {}, 2)
check("换装：以 image_2 为服装来源", "image_2 中的服装" in outfit["prompt"])
check("换装：含三不变", "不作重新构图或整体重绘" in outfit["prompt"])

txt = portrait.build_prompt("editorial_portrait", {"subject": "a botanist in a greenhouse"}, 0)
check("文生图：无参考图也可组装", "editorial portrait" in txt["prompt"])
check("文生图：kind=txt2img", txt["kind"] == "txt2img")

# ============================================================
# 3. 校验
# ============================================================
try:
    portrait.build_prompt("portrait_scene", {}, 1)
    check("必填缺失应报错", False)
except PortraitError as exc:
    check("必填缺失报错文案正确", "请填写" in str(exc))

try:
    portrait.build_prompt("nope", {}, 1)
    check("未知玩法应报错", False)
except PortraitError:
    check("未知玩法报错", True)

try:
    portrait.build_prompt("multi_compose", {"compose": "x"}, 1)
    check("参考图不足应报错", False)
except PortraitError as exc:
    check("参考图不足报错文案正确", "需要 2~3 张" in str(exc))

over = portrait.build_prompt("outfit_swap", {}, 3)
check("超出最大张数给出提示", any("最多使用 2 张" in w for w in over["warnings"]))
check("编辑类提示 CFG=1 负面词不生效", any("CFG=1" in w for w in over["warnings"]))
check("编辑类提示输出尺寸由 image_1 决定",
      any("由 image_1" in w for w in over["warnings"]))

# 预览宽容模式：还没上传参考图也能组装（前端选完玩法就要看到提示词）
loose = portrait.build_prompt("portrait_scene", {"scene": "火星表面的红色荒漠"}, 0, strict=False)
check("预览模式：未上传参考图仍能组装", "火星表面的红色荒漠" in loose["prompt"])
check("预览模式：给出缺图提示而非报错",
      any("需要 1 张参考图" in w for w in loose["warnings"]))
check("预览模式：缺图时仍按 image_1 措辞组装", "image_1" in loose["prompt"])

# ============================================================
# 4. 参考图归一化
# ============================================================
big = Image.new("RGB", (3000, 2000), (10, 20, 30))
small = portrait.normalize_reference(big, max_mp=0.8)
mp = small.size[0] * small.size[1] / 1e6
check("归一化：像素预算 <= 0.8MP", mp <= 0.8 + 1e-6)
check("归一化：最长边 <= 1280", max(small.size) <= 1280)
check("归一化：边长是 64 的倍数",
      small.size[0] % 64 == 0 and small.size[1] % 64 == 0)
check("归一化：保持横向比例", small.size[0] > small.size[1])

tiny = Image.new("RGB", (512, 640), (0, 0, 0))
check("归一化：已达标图片不做改动", portrait.normalize_reference(tiny, 0.8).size == (512, 640))

refs, notes = portrait.prepare_references([data_url((1500, 1500))], max_mp=0.8)
check("prepare_references 返回 1 张", len(refs) == 1)
check("prepare_references 返回 data URL", refs[0].startswith("data:image/png;base64,"))
check("prepare_references 给出缩放说明", notes and "image_1" in notes[0])

# ============================================================
# 5. 出图 payload（走多参考图编辑通道）
# ============================================================
built = portrait.build_generation_payload(
    preset_id="outfit_swap",
    values={},
    images_data=[data_url((1024, 1024)), data_url((800, 800), (90, 90, 90))],
    steps=8, seed=1234, count=1, max_mp=0.8,
)
pl = built["payload"]
check("编辑玩法 mode=img2img", pl["mode"] == "img2img")
check("编辑玩法 fusion=True（→ 多参考图编辑通道）", pl.get("fusion") is True)
check("参考图数量为 2", len(pl["ref_images"]) == 2)
check("参考图为 data URL", all(r.startswith("data:image/") for r in pl["ref_images"]))
check("编辑玩法 CFG 记为 1.0（引擎固定值）", pl["guidance_scale"] == 1.0)
check("负面提示词留空（CFG=1 下不生效）", pl["negative_prompt"] == "")
# 1024×1024 = 1.05MP 超出 0.8MP 预算 → 会被压到 896×896（64 的倍数）
check("输出尺寸跟随 image_1（经像素预算归一化）",
      (pl["width"], pl["height"]) == (896, 896))
check("按玩法归档到 outputs/portrait/日期",
      pl["output_subdir"].startswith("portrait/"))
check("payload 带玩法标记", pl["portrait"]["preset"] == "outfit_swap")
check("未污染传统单图图生图字段", pl["image"] is None)

too_many = portrait.build_generation_payload(
    preset_id="outfit_swap", values={},
    images_data=[data_url(), data_url(), data_url()], max_mp=0.4,
)
check("超出上限的参考图被截断", len(too_many["payload"]["ref_images"]) == 2)
check("截断给出提示", any("最多使用 2 张" in w for w in too_many["warnings"]))

txt_pl = portrait.build_generation_payload(
    preset_id="editorial_portrait",
    values={"subject": "a botanist in a sunlit greenhouse"},
    images_data=[], steps=8, seed=-1, count=1, width=768, height=1024,
)
check("文生图玩法 mode=txt2img", txt_pl["payload"]["mode"] == "txt2img")
check("文生图玩法不带参考图", "ref_images" not in txt_pl["payload"])

# 与既有口径的一致性：漫画分镜融合仍要求 mode=img2img + fusion
from app.services.generation import _prepare_references  # noqa: E402

check("generation 能解析人像 payload 的参考图列表",
      len(_prepare_references(pl)) == 2)
check("传统图生图（无 fusion）只取单张",
      len(_prepare_references({"mode": "img2img", "image": data_url(), "ref_images": []})) == 1)

# ============================================================
# 6. blobs 外置往返（MB 级 base64 不进数据库）
# ============================================================
job_id = "job_portrait_test"
big_pl = dict(pl)
big_pl["ref_images"] = [noisy_data_url((768, 768)), noisy_data_url((640, 640))]
big_pl["prompt"] = pl["prompt"]
packed = blobs.dumps(big_pl, job_id)
check("pack 后 params_json 不留 base64 原文",
      "iVBORw0KGgo" not in packed and packed.count("@blob:") == 2)
check("pack 后单条参数大小可控（KB 级）", len(packed) < 8000)
restored = blobs.loads(packed)
check("unpack 后参考图完整还原", restored["ref_images"] == big_pl["ref_images"])
check("unpack 后提示词完整", restored["prompt"] == pl["prompt"])
blobs.release(job_id)

# ============================================================
# 7. API 层（拦截出图提交，不真正入队）
# ============================================================
captured = {}


async def fake_submit(payload):
    captured["payload"] = payload
    return {"job_id": payload["job_id"], "status": "queued", "position": 1, "queue_size": 1}


generation.submit_batch = fake_submit  # type: ignore[assignment]


async def api_flow():
    info = await portrait_api.compose(PortraitComposeIn(
        preset="portrait_hair", values={"hairstyle": "大波浪卷发"}, image_count=1
    ))
    check("API compose 返回提示词", "大波浪卷发" in info["prompt"])
    check("API compose 返回分段", len(info["sections"]) == 4)

    res = await portrait_api.submit(PortraitSubmitIn(
        preset="portrait_hair", values={"hairstyle": "大波浪卷发"},
        images=[data_url((768, 1024))], steps=8, seed=7, count=1,
        width=768, height=1024, max_mp=0.8,
    ))
    check("API submit 返回 job_id", bool(res.get("job_id")))
    check("API submit 回传出图尺寸（768×1024 在预算内保持不变）",
          res["width"] == 768 and res["height"] == 1024)
    check("API submit 复用出图队列", captured["payload"]["mode"] == "img2img"
          and captured["payload"].get("fusion") is True)
    check("API submit 注入 job_id", captured["payload"]["job_id"] == res["job_id"])

    bad = None
    try:
        await portrait_api.submit(PortraitSubmitIn(
            preset="multi_compose", values={"compose": "x"}, images=[], steps=8,
        ))
    except Exception as exc:  # HTTPException
        bad = exc
    check("API 参考图不足返回 400",
          bad is not None and getattr(bad, "status_code", None) == 400)


asyncio.run(api_flow())

# ============================================================
print()
if fails:
    print(f"共 {len(fails)} 项失败：")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("全部通过 ✔")
