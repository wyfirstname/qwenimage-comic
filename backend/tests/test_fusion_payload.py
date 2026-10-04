# -*- coding: utf-8 -*-
"""回归测试：分镜必须走 Qwen-Image 2.1 多参考图融合通道（payload/提示词/槽位一致性）。

运行：backend> ..\.venv\Scripts\python.exe tests\test_fusion_payload.py
不依赖模型与数据库：用临时库 + 拦截队列提交，只校验口径。
"""
import asyncio
import base64
import io
import json
import os
import sys
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp(prefix="fusion_test_"))
os.environ["DB_PATH"] = str(tmp / "t.db")
os.environ["DATA_DIR"] = str(tmp / "data")
os.environ["OUTPUT_DIR"] = str(tmp / "data/outputs")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db as appdb  # noqa: E402
appdb.db

from app import comic_repo as crepo  # noqa: E402
from app.services import comic as cs  # noqa: E402
from app.services import generation  # noqa: E402
from PIL import Image  # noqa: E402

fails = []


def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        fails.append(label)


pid = crepo.create_project(title="融合测试", width=768, height=768, steps=8,
                           guidance=4.0)["id"]
char_ids = []
for name in ("牛郎", "织女"):
    p = tmp / f"{name}.png"
    Image.new("RGB", (256, 512), (190, 190, 200)).save(p)
    cid = crepo.create_character(pid, name=name)["id"]
    crepo.update_character(cid, ref_path=str(p), detail_prompt="tall, dark hair")
    char_ids.append(cid)

# 场景图故意用横向 768x576，验证会被铺满裁到项目尺寸（image_1 决定输出尺寸）
scene_png = tmp / "scene.png"
Image.new("RGB", (768, 576), (90, 120, 160)).save(scene_png)
sid = crepo.create_scene(pid, name="鹊桥", location="银河鹊桥", time_of_day="夜")["id"]
crepo.update_scene(sid, status="done", image_url=str(scene_png))

panel_id = crepo.create_panel(pid, seq=1, page=1, cell=0, shot="wide",
                              scene="牛郎织女立于鹊桥两端，衣袂飘摇",
                              dialogue="我们终于见到了", character_ids=char_ids,
                              scene_id=sid, status="draft")["id"]

captured = {}


async def fake_submit(payload):
    captured["payload"] = payload
    return {"queued": 1}


generation.submit_batch = fake_submit  # type: ignore[assignment]

res = asyncio.run(cs.enqueue_panel(panel_id, reseed=True))
p = captured["payload"]

check("mode=img2img（复用主队列通道）", p.get("mode") == "img2img")
check("fusion 标记置位", p.get("fusion") is True)
check("CFG 固定 1.0（官方编辑路径）", abs(float(p["guidance_scale"]) - 1.0) < 1e-6)
check("参考文献数量=场景+2角色", len(p.get("ref_images") or []) == 3)
check("image 字段=第一张（image_1）", p.get("image") == p["ref_images"][0])

slots = p.get("ref_slots") or []
check("槽位顺序：image_1=场景", slots and slots[0]["slot"] == "image_1"
      and slots[0]["kind"] == "scene")
check("槽位顺序：image_2/3=牛郎/织女",
      [s["name"] for s in slots[1:]] == ["牛郎", "织女"])

# 解出第一张参考图，确认尺寸被铺满为项目尺寸（决定输出尺寸）
raw = base64.b64decode(p["ref_images"][0].split(",", 1)[1])
first = Image.open(io.BytesIO(raw))
check("image_1 尺寸=768x768（锁定输出尺寸）", first.size == (768, 768))

prompt = p["prompt"]
check("提示词含 image_1 场景引用", "image_1" in prompt)
check("提示词含人物按序引用", "image_2 is 牛郎" in prompt and "image_3 is 织女" in prompt)
check("提示词含融合约束（不得拼贴）",
      "no collage" in prompt and "no side-by-side portrait layout" in prompt)
check("提示词含保持形象约束", "Keep every character's face" in prompt)

info = cs.panel_generation_info(panel_id)
check("词面板：fusion=True", info["fusion"] is True)
check("词面板：cfg=1.0", abs(info["cfg"] - 1.0) < 1e-6)
check("词面板：槽位数=3", len(info["ref_slots"]) == 3)

# 没有场景图时：image_1 应是首个角色立绘并装入项目画布
crepo.update_scene(sid, status="queued")
captured.clear()
asyncio.run(cs.enqueue_panel(panel_id, reseed=True))
p2 = captured["payload"]
slots2 = p2.get("ref_slots") or []
check("无场景图：image_1=首个角色", slots2 and slots2[0]["kind"] == "portrait"
      and slots2[0]["name"] == "牛郎")
raw2 = base64.b64decode(p2["ref_images"][0].split(",", 1)[1])
check("无场景图：image_1 仍为项目尺寸", Image.open(io.BytesIO(raw2)).size == (768, 768))

# 纯文生图分支：use_ref 关掉后不应出现 image_N 措辞
crepo.update_scene(sid, status="done")
crepo.update_project(pid, use_ref=0)
captured.clear()
asyncio.run(cs.enqueue_panel(panel_id, reseed=True))
p3 = captured["payload"]
check("关闭形象参考→txt2img", p3.get("mode") == "txt2img" and not p3.get("fusion"))
check("txt2img 提示词不含 image_N", "image_1" not in p3["prompt"])
check("txt2img 沿用项目 CFG", abs(float(p3["guidance_scale"]) - 4.0) < 1e-6)

# 边界：场景状态 done 但图片文件缺失 → image_1 应落到首个立绘，且槽位标签同步
crepo.update_project(pid, use_ref=1)
crepo.update_scene(sid, image_url=str(tmp / "missing.png"))
captured.clear()
asyncio.run(cs.enqueue_panel(panel_id, reseed=True))
p4 = captured["payload"]
s4 = p4.get("ref_slots") or []
check("场景文件缺失：image_1 降级为立绘", s4 and s4[0]["slot"] == "image_1"
      and s4[0]["kind"] == "portrait" and s4[0]["name"] == "牛郎")
check("场景文件缺失：槽位与提示词一致",
      "image_1 is 牛郎" in p4["prompt"] and "image_2 is 织女" in p4["prompt"])
check("场景文件缺失：仍为融合模式", p4.get("fusion") is True and len(p4["ref_images"]) == 2)

# 边界：立绘全部缺失（换电脑后绝对路径失效的典型情况）→ 即使场景图完好，
# 也必须退回文生图。绝不能做"纯场景"融合：模型拿到"保持 image_1 构图"的
# 指令却没有任何人物参考，会原样复刻空背景，人物根本进不来。
crepo.update_scene(sid, image_url=str(scene_png))
for cid in char_ids:
    crepo.update_character(cid, ref_path=str(tmp / "gone.png"))
captured.clear()
asyncio.run(cs.enqueue_panel(panel_id, reseed=True))
p5 = captured["payload"]
check("立绘全缺失→txt2img（不做纯场景融合）",
      p5.get("mode") == "txt2img" and not p5.get("fusion"))
check("立绘全缺失：提示词仍描述角色（人物靠文字生成）",
      "牛郎" in p5["prompt"] and "image_1" not in p5["prompt"])

print()
print("FAILED:", fails) if fails else print("ALL PASS")
sys.exit(1 if fails else 0)
