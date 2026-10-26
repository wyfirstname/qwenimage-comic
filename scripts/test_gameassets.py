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


def main_v6():
    """6.0 自测：主角性别展开 / 按类提示词 / 分类页签数据结构。"""
    failures = []
    from apps.gameassets.backend import pipeline
    from apps.gameassets.backend.presets import (build_stage_items, entry_genders,
                                                 prompt_extra, prompt_negative)

    # 1) 性别推断：主角男女、女主角只女、NPC 默认男、显式覆盖优先
    cases = [({"name": "主角·战士"}, ["male", "female"]),
             ({"name": "女主角·游侠"}, ["female"]),
             ({"name": "导师·长老"}, ["male"]),
             ({"name": "商人", "genders": ["female", "male"]}, ["male", "female"]),
             ({"name": "路人", "genders": []}, ["male"])]
    for entry, want in cases:
        got = entry_genders(entry)
        if got != want:
            failures.append(f"性别推断 {entry} 期望 {want} 实际 {got}")

    # 2) 条目展开：主角出两条（男女各一），NPC 出一条
    plan = {"scenes": [], "characters": [
                {"name": "主角·剑士", "desc": "d1"},
                {"name": "女主角·术士", "desc": "d2"},
                {"name": "铁匠", "desc": "d3"}],
            "pets": [], "weapons": [], "buildings": [], "monsters": []}
    items = build_stage_items(plan)["character"]
    if len(items) != 4:
        failures.append(f"角色条目应为 4 条（2+1+1），实际 {len(items)}")
    gset = sorted({(it["name"], it.get("gender")) for it in items})
    if ("主角·剑士", "male") not in gset or ("主角·剑士", "female") not in gset:
        failures.append(f"主角应展开男女两条：{gset}")
    if len({it["id"] for it in items}) != len(items):
        failures.append("条目 id 不唯一")

    # 3) 稳定 id：同一方案重复展开，id 不变（对账靠它）
    again = build_stage_items(plan, {"character": items})["character"]
    if [it["id"] for it in again] != [it["id"] for it in items]:
        failures.append("重复展开 id 不稳定")

    # 4) 提示词：性别词进前置主体位、按类补充词生效
    s = {"style": "ghibli", "time_preset": "dawn", "desc": "",
         "prompts": {"character": {"extra": "jade armor inlay", "negative": "modern gun"}}}
    pm = pipeline.asset_prompt(s, {"kind": "character", "name": "剑士", "desc": "重甲",
                                   "gender": "female"})
    if "female character" not in pm:
        failures.append("女版提示词缺性别词")
    if "jade armor inlay" not in pm:
        failures.append("按类正向补充词未拼入")
    pm2 = pipeline.asset_prompt(s, {"kind": "pet", "name": "灵狐", "desc": "银白"})
    if "jade armor inlay" in pm2:
        failures.append("角色补充词串到了宠物类")
    neg = pipeline.asset_negative(s, "character")
    if "modern gun" not in neg:
        failures.append(f"按类负面词未拼入：{neg}")
    if "modern gun" in pipeline.asset_negative(s, "pet"):
        failures.append("角色负面词串到了宠物类")

    # 5) 取值助手：空值安全
    if prompt_extra({}, "pet") != "" or prompt_negative({"prompts": None}, "pet") != "":
        failures.append("提示词助手空值应返回空串")

    if failures:
        print("FAIL:")
        for f in failures:
            print("  -", f)
        return 1
    print("V6 ALL PASS")
    return 0


def main_v7():
    """7.0 自测：场景用途→画幅 / 怪物等级→画幅 / 场景↔怪物配属。"""
    failures = []
    from apps.gameassets.backend import pipeline
    from apps.gameassets.backend.presets import (ASPECTS, MONSTER_TIERS,
                                                 MONSTER_TIER_ORDER,
                                                 SCENE_USAGES, SCENE_USAGE_ORDER,
                                                 asset_size, build_stage_items,
                                                 monster_tier, scene_usage)

    # 1) 每个场景用途都必须给出「尺寸 + 理由」，且 64 对齐
    for uid in SCENE_USAGE_ORDER:
        u = SCENE_USAGES.get(uid)
        if not u:
            failures.append(f"场景用途缺失：{uid}")
            continue
        w, h = asset_size({}, "scene", {"usage": uid})
        if [w, h] != list(u["size"]) or w % 64 or h % 64:
            failures.append(f"{uid} 画幅不对：{w}x{h} 期望 {u['size']}")
        if not str(u.get("reason") or "").strip() or not str(u.get("aspect") or "").strip():
            failures.append(f"{uid} 缺尺寸理由或比例标注")
    if len({tuple(SCENE_USAGES[k]["size"]) for k in SCENE_USAGE_ORDER}) < 4:
        failures.append("场景用途的画幅过于单一（至少要区分横/竖/方/超宽）")

    # 2) 怪物四档等级，画幅随等级递增，且都有理由
    if MONSTER_TIER_ORDER != ["minion", "elite", "boss", "worldboss"]:
        failures.append(f"怪物等级顺序异常：{MONSTER_TIER_ORDER}")
    prev = 0
    for tid in MONSTER_TIER_ORDER:
        t = MONSTER_TIERS[tid]
        w, h = asset_size({}, "monster", {"tier_id": tid})
        if w != h or [w, h] != list(t["size"]) or w % 64:
            failures.append(f"{tid} 画幅不对：{w}x{h} 期望 {t['size']}")
        if w <= prev:
            failures.append(f"{tid} 画幅没随等级放大（{w} <= {prev}）")
        prev = w
        if not str(t.get("note") or "").strip():
            failures.append(f"{tid} 缺画幅理由")
        if not str(t.get("prompt") or "").strip():
            failures.append(f"{tid} 缺等级提示词")

    # 3) 条目携带用途/等级/配属，且提示词真的用上
    plan = {"scenes": [{"name": "码头", "desc": "d1", "usage": "banner"},
                       {"name": "沼泽", "desc": "d2", "usage": "tile"}],
            "characters": [], "pets": [], "weapons": [], "buildings": [],
            "monsters": [{"name": "沼蛭", "desc": "m1", "tier_id": "elite", "scene": "沼泽"},
                         {"name": "沼王", "desc": "m2", "tier_id": "worldboss",
                          "scene": "沼泽"}]}
    items = build_stage_items(plan)
    sc = items["scene"][0]
    if sc.get("usage") != "banner" or sc.get("tileable"):
        failures.append(f"场景条目用途未落库：{sc}")
    if asset_size({}, "scene", sc) != (1152, 512):
        failures.append(f"banner 画幅应为 1152×512，实际 {asset_size({}, 'scene', sc)}")
    mo = items["monster"][0]
    if mo.get("tier") != "精英" or mo.get("scene") != "沼泽":
        failures.append(f"怪物等级/配属未落库：{mo}")
    s = {"style": "ghibli", "time_preset": "dusk", "desc": "", "prompts": {}}
    p1 = pipeline.asset_prompt(s, dict(items["scene"][0], kind="scene"))
    if "panoramic" not in p1:
        failures.append("横幅场景的构图提示未进提示词")
    p2 = pipeline.asset_prompt(s, dict(items["monster"][1], kind="monster"))
    if "colossus" not in p2 or "沼泽" not in p2:
        failures.append(f"终局怪缺等级词或出没场景：{p2[-160:]}")
    p3 = pipeline.asset_prompt(s, dict(items["scene"][4], kind="scene")
                               if len(items["scene"]) > 4 else
                               {"kind": "scene", "name": "沼泽", "desc": "d",
                                "usage": "tile"})
    if "seamless tileable" not in p3:
        failures.append("可平铺场景缺无缝提示")

    # 4) 老数据兼容：只有 tileable 没有 usage 时按平铺处理
    if scene_usage({"tileable": True}) != "tile" or scene_usage({}) != "battle":
        failures.append("老数据用途回退不对")
    if monster_tier({}) != "minion" or monster_tier({"tier_id": "boss"}) != "boss":
        failures.append("怪物等级回退不对")

    # 5) 改用途/等级必须触发重置（不能沿用旧成图）
    again = build_stage_items({**plan, "monsters": [
        {"name": "沼蛭", "desc": "m1", "tier_id": "minion", "scene": "沼泽"}]},
        {"monster": [{"id": "itm_x", "key": "0", "name": "沼蛭", "desc": "m1",
                      "tier_id": "elite", "tier": "精英", "scene": "沼泽",
                      "status": "done"}]})
    if again["monster"][0]["status"] != "pending":
        failures.append("改怪物等级后条目未重置为待生成")
    again2 = build_stage_items({**plan, "scenes": [
        {"name": "码头", "desc": "d1", "usage": "portrait"}]},
        {"scene": [{"id": "itm_s", "key": "0:spring", "name": "码头", "desc": "d1",
                    "season": "spring", "usage": "banner", "status": "done"}]})
    if again2["scene"][0]["status"] != "pending":
        failures.append("改场景用途后条目未重置为待生成")

    if failures:
        print("FAIL:")
        for f in failures:
            print("  -", f)
        return 1
    print("V7 ALL PASS")
    return 0


if __name__ == "__main__":
    rc = main()
    rc2 = main_v5()
    rc3 = main_v6()
    rc4 = main_v7()
    sys.exit(rc or rc2 or rc3 or rc4)
