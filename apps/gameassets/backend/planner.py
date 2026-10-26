# -*- coding: utf-8 -*-
"""自动策划器：用户一句话 → 完整素材方案。

两条路：
  1. LLM 策划（首选）：engine.text_generate 让文本模型当策划，
     输出严格 JSON 方案。16G 机器有 ComfyUI 文本通道时走这条，方案贴题。
  2. 规则策划（兜底）：LLM 不可用（无 ComfyUI / 解析失败 / 超时）时，
     用固定模板套用户的一句话。名字普通但结构完整，流水线照样能跑。

两条路产出同一结构，数量由规模档（SCALES）统一约束，超了裁掉、缺了不补。
"""

from __future__ import annotations

import json
import re

from engine.sdk import engine

from .presets import (MONSTER_TIER_ORDER, SEASON_ORDER, TIER_ORDER,
                      SCENE_USAGE_ORDER)

# ------------------------------------------------------------
#  规模档：每类数量（场景条数 ×4 季 = 场景图数）
# ------------------------------------------------------------
SCALES: dict[str, dict] = {
    "lite":     {"name": "精简包", "scenes": 2, "characters": 2, "pets": 1,
                 "weapons": 2, "buildings": 1, "monsters": 2},
    "standard": {"name": "标准包", "scenes": 3, "characters": 3, "pets": 2,
                 "weapons": 3, "buildings": 2, "monsters": 3},
    "deluxe":   {"name": "豪华包", "scenes": 4, "characters": 4, "pets": 3,
                 "weapons": 4, "buildings": 3, "monsters": 4},
}

_PLAN_SCHEMA = """{
  "title": "游戏名",
  "synopsis": "两三句世界观",
  "scenes": [{"name": "场景名", "usage": "battle|tile|portrait|banner|iso", "desc": "外观描述，15-40字"}],
  "characters": [{"name": "角色名", "gender": "男|女|男女", "desc": "外形/服装/气质描述，15-40字"}],
  "pets": [{"name": "宠物名", "desc": "物种/体型/特征描述，15-40字"}],
  "weapons": [{"name": "武器名", "tier": "普通|精良|史诗|传说", "desc": "形制/材质描述，15-40字"}],
  "buildings": [{"name": "建筑名", "desc": "结构/材质/氛围描述，15-40字"}],
  "monsters": [{"name": "野怪名", "tier": "杂兵|精英|首领|终局", "scene": "出没场景名（必须是上面场景之一）", "desc": "物种/体型/威胁感描述，15-40字"}]
}"""

_USAGE_ALIAS = {
    "battle": "battle", "横版战斗背景": "battle", "战斗背景": "battle",
    "战斗": "battle", "16:9": "battle", "横版": "battle",
    "tile": "tile", "可平铺地表": "tile", "可平铺": "tile", "平铺": "tile",
    "地表": "tile", "材质": "tile",
    "portrait": "portrait", "竖屏副本": "portrait", "竖屏": "portrait",
    "手机": "portrait", "9:16": "portrait",
    "banner": "banner", "章节横幅": "banner", "横幅": "banner", "加载页": "banner",
    "iso": "iso", "等距斜视小场景": "iso", "等距": "iso", "战棋": "iso",
    "斜视": "iso",
}

_TIER_ALIAS = {
    "minion": "minion", "杂兵": "minion", "小怪": "minion", "普通": "minion",
    "普通怪": "minion", "群体": "minion",
    "elite": "elite", "精英": "elite", "精英怪": "elite", "精良": "elite",
    "boss": "boss", "首领": "boss", "BOSS": "boss", "boss怪": "boss", "精英首领": "boss",
    "worldboss": "worldboss", "终局": "worldboss", "世界首领": "worldboss",
    "世界boss": "worldboss", "传说": "worldboss", "终极": "worldboss",
}


def _resolve(aliaz: dict, raw: str, fallback: str) -> str:
    """把模型给的中文/英文标签归一到 id：优先精确命中，其次取最长包含项。"""
    key = str(raw or "").strip().lower()
    if not key:
        return fallback
    if key in aliaz:
        return aliaz[key]
    hits = [(len(k), v) for k, v in aliaz.items() if k in key]
    return max(hits)[1] if hits else fallback


def plan_from_idea(idea: str, scale_id: str) -> dict:
    """主入口：返回 {title, synopsis, scale, items:{...}}。永不抛异常（兜底兜住）。"""
    scale = SCALES.get(scale_id, SCALES["standard"])
    plan, source = None, "rules"
    # 守门：只在文本引擎真可用时才尝试 LLM。
    # 不能裸调 generate——ComfyUI 没运行时它会尝试拉起引擎（1-2 分钟），
    # 把一次「点按钮出方案」变成卡死两分钟，这是体验事故。
    try:
        st = engine.text_status()
        if not st.get("available"):
            raise RuntimeError(st.get("reason") or "文本引擎不可用")
        raw = engine.text_generate(
            _llm_prompt(idea, scale),
            system=("你是资深游戏策划。只输出一个 JSON 对象，不要任何解释、不要代码块标记。"
                    "desc 必须是可直接入画的视觉描述（外观/材质/颜色/姿态），不要剧情背景。"),
            max_length=1800,
            temperature=0.8,
        )
        plan = _parse_plan(raw)
        source = "llm"
    except Exception as exc:
        print(f"[gameassets] LLM 策划不可用，走规则兜底：{type(exc).__name__}: {exc}")
    if plan is None:
        plan = _rule_plan(idea)

    plan = _normalize(plan, scale)
    plan["scale"] = scale_id
    plan["scale_name"] = scale["name"]
    plan["planner"] = source
    return plan


# ------------------------------------------------------------
#  LLM 策划
# ------------------------------------------------------------

def _llm_prompt(idea: str, scale: dict) -> str:
    return (
        f"用户想做的游戏：{idea.strip()}\n\n"
        f"请输出美术素材方案 JSON，结构与示例完全一致：\n{_PLAN_SCHEMA}\n\n"
        f"数量硬性要求：场景 {scale['scenes']} 个、角色 {scale['characters']} 个、"
        f"宠物 {scale['pets']} 只、武器 {scale['weapons']} 把（等级从「普通」起至少覆盖到"
        f"第 {scale['weapons']} 级，等级只用：普通/精良/史诗/传说）、"
        f"建筑 {scale['buildings']} 座、野怪 {scale['monsters']} 种。\n"
        "角色里至少一位是「主角」，主角的 gender 必须是「男女」（系统会生成男女两版），"
        "其余角色按设定填「男」或「女」。\n"
        "场景每个必须给 usage，按它在游戏里的实际用途选，不要全填 battle：\n"
        "  battle=横版战斗背景(16:9 1152×640) / tile=可平铺地表(1:1 1024) / "
        "portrait=竖屏副本(2:3 768×1152) / banner=加载页章节横幅(9:4 1152×512) / "
        "iso=战棋等距战场(1:1 896)。至少有一个是可玩战斗场景。\n"
        "野怪必须分级，等级只用：杂兵/精英/首领/终局，且从杂兵起逐级覆盖"
        f"（{scale['monsters']} 只就从杂兵开始往上排），每级体型威胁感要有明显差别；"
        "每只野怪必须写 scene（出没场景，必须精确等于上面某个场景的 name），"
        "尽量让每个场景都有对应的怪，同一场景允许不止一只。\n"
        "名字与描述要贴合用户的游戏题材，不要用示例里的占位词。"
    )


def _parse_plan(raw: str) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    # 剥掉 ```json 围栏与前后废话，取最外层大括号
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("scenes"):
        return None
    return data


# ------------------------------------------------------------
#  规则策划（兜底模板）
# ------------------------------------------------------------

_FALLBACK_TPL = {
    "title": "新世界",
    "synopsis": "",
    # (名称, 用途, 描述) —— 用途决定画幅，见 presets.SCENE_USAGES
    "scenes": [
        ("起点村庄", "battle", "依山而建的石头村庄，炊烟袅袅，木质牌坊立在村口"),
        ("荒野古道", "battle", "风蚀岩丘与枯草平原，远处矗立着残破的界碑"),
        ("村外农田", "tile", "规整的田垄与低矮石埂，灌溉水渠纵横交错"),
        ("古神祭坛", "iso", "悬浮的古代遗迹群，符文石柱环绕中央祭坛"),
    ],
    "characters": [
        ("主角·战士", "身披重型铠甲的年轻战士，肩披染风尘的斗篷，手持巨剑", "男女"),
        ("主角·法师", "身穿绣纹长袍的法师，手持木质法杖，腰间挂着卷轴", "男女"),
        ("女主角·游侠", "轻装皮甲的游侠少女，背挎长弓，发间系着红色发带", "女"),
        ("导师·长老", "白发白须的长老，粗布长袍，手持弯曲木杖，眼神慈祥", "男"),
    ],
    "pets": [
        ("幼年灵狐", "巴掌大的银白色狐狸，尾巴尖泛着微光，大眼睛"),
        ("岩甲龟", "背着青灰色岩壳的小龟，壳上长着苔藓，步态憨厚"),
        ("风隼", "羽翼带蓝纹的猎隼，收拢翅膀立在皮质护腕上"),
    ],
    "weapons": [
        ("新手木剑", "普通", "训练用的木剑，剑身有磕痕，缠着旧布柄"),
        ("铁刃长剑", "精良", "锻造精良的铁剑，剑脊笔直，护手刻着细密纹路"),
        ("符文魔剑", "史诗", "剑身嵌着发光符文的魔剑，蓝光沿刃流动"),
        ("龙魂神刃", "传说", "传说级神刃，金纹缠绕宝石剑柄，刃口萦绕龙形光焰"),
    ],
    "buildings": [
        ("铁匠铺", "半开放式铁匠铺，炉火通红，门口挂着成排的铁器"),
        ("传送塔", "高耸的石头法阵塔，塔顶悬浮着旋转的光环"),
        ("村长会堂", "厚重的木石会堂，横梁上挂着兽骨与地图"),
    ],
    # (名称, 等级, 出没场景, 描述) —— 等级决定画幅，场景是配属关系
    "monsters": [
        ("荒野狼群", "杂兵", "起点村庄", "灰毛野狼，压低前身龇牙，眼睛在阴影里发亮"),
        ("石像守卫", "精英", "荒野古道", "半人高石像怪，苔藓覆肩，双臂是粗重的石锤"),
        ("腐沼巨蟾", "首领", "村外农田", "沼泽巨蟾，背部疣突冒着毒泡，长舌垂地"),
        ("深渊守望者", "终局", "古神祭坛", "披着破碎黑袍的巨影，六条手臂各持一柄裂刃"),
    ],
}


def _rule_plan(idea: str) -> dict:
    idea = idea.strip() or "奇幻冒险"
    plan = {"title": f"{idea[:12]}", "synopsis": f"一个关于{idea}的冒险世界。"}
    for kind, entries in _FALLBACK_TPL.items():
        if kind in ("title", "synopsis"):
            continue
        items = []
        for e in entries:
            if kind == "weapons":          # (name, tier, desc)
                n, t, d = e
                items.append({"name": n, "tier": t, "desc": f"{d}（{idea}风格）"})
            elif kind == "characters":     # (name, desc, gender)
                n, d, g = e
                items.append({"name": n, "desc": f"{d}（{idea}风格）", "gender": g})
            elif kind == "scenes":         # (name, usage, desc)
                n, u, d = e
                items.append({"name": n, "usage": u, "desc": f"{d}（{idea}风格）"})
            elif kind == "monsters":       # (name, tier, scene, desc)
                n, t, sc, d = e
                items.append({"name": n, "tier": t, "scene": sc,
                              "desc": f"{d}（{idea}风格）"})
            else:                          # (name, desc)
                n, d = e
                items.append({"name": n, "desc": f"{d}（{idea}风格）"})
        plan[kind] = items
    return plan


def _parse_genders(it: dict, name: str) -> list[str]:
    """模型给的 gender 字段（男/女/男女/英文）→ ['male'] / ['female'] / 两个都有。

    没给就按名字猜（女主 → 女；主角 → 男女；其它 → 男），与 presets.entry_genders 一致。
    """
    raw = str(it.get("gender") or it.get("genders") or "").strip().lower()
    if raw:
        both = ("男女" in raw or "both" in raw or "all" in raw)
        if both:
            return ["male", "female"]
        if "女" in raw or "female" in raw or raw in ("f", "woman"):
            return ["female"]
        if "男" in raw or "male" in raw or raw in ("m", "man"):
            return ["male"]
    from .presets import entry_genders
    return entry_genders({"name": name})


# ------------------------------------------------------------
#  归一化：裁数量、补字段、控长度 —— 无论 LLM 还是规则，出口结构一致
# ------------------------------------------------------------

def _normalize(plan: dict, scale: dict) -> dict:
    out = {
        "title": str(plan.get("title") or "未命名世界")[:40],
        "synopsis": str(plan.get("synopsis") or "")[:200],
    }
    spec = {"scenes": scale["scenes"], "characters": scale["characters"],
            "pets": scale["pets"], "weapons": scale["weapons"],
            "buildings": scale["buildings"], "monsters": scale["monsters"]}
    for kind, limit in spec.items():
        items = plan.get(kind) or []
        norm = []
        for it in items[:limit]:
            if isinstance(it, str):        # 容错：模型偷懒只给了字符串
                it = {"name": it, "desc": it}
            name = str(it.get("name") or "").strip()[:30]
            desc = str(it.get("desc") or "").strip()[:80]
            if not name:
                continue
            entry = {"name": name, "desc": desc or name}
            if kind == "characters":
                entry["genders"] = _parse_genders(it, name)
                entry["gender_label"] = "/".join(
                    {"male": "男", "female": "女"}[g] for g in entry["genders"])
            if kind == "weapons":
                tier = str(it.get("tier") or "普通")
                tier_id = {"普通": "common", "精良": "fine",
                           "史诗": "epic", "传说": "legendary"}.get(tier, "common")
                entry["tier"] = {"common": "普通", "fine": "精良",
                                 "epic": "史诗", "legendary": "传说"}[tier_id]
                entry["tier_id"] = tier_id
            if kind == "scenes":
                # 用途必须落在一个已知画幅上（大小有据可查）
                usage = _resolve(_USAGE_ALIAS, it.get("usage"), "battle")
                entry["usage"] = usage
                entry["tileable"] = usage == "tile"
            if kind == "monsters":
                tier_id = _resolve(_TIER_ALIAS, it.get("tier"), "minion")
                entry["tier_id"] = tier_id
                entry["tier"] = {"minion": "杂兵", "elite": "精英",
                                 "boss": "首领", "worldboss": "终局"}[tier_id]
                entry["scene"] = str(it.get("scene") or "").strip()[:30]
            norm.append(entry)
        if kind == "scenes" and not norm:
            norm = [{"name": "主场景", "desc": plan.get("synopsis") or out["title"],
                     "usage": "battle", "tileable": False}]
        out[kind] = norm
    # 武器按等级排序（普通在前），确保等级覆盖
    out["weapons"].sort(key=lambda w: TIER_ORDER.index(w.get("tier_id", "common")))
    # 野怪按等级排序（杂兵→终局），并补齐「出没场景」的配属关系
    scene_names = [sc["name"] for sc in out["scenes"]]
    for i, m in enumerate(out["monsters"]):
        want = m.get("scene") or ""
        hit = next((n for n in scene_names if n == want), None) \
            or next((n for n in scene_names if want and (want in n or n in want)), None)
        m["scene"] = hit or scene_names[i % len(scene_names)]
    out["monsters"].sort(key=lambda m: MONSTER_TIER_ORDER.index(
        m.get("tier_id", "minion")))
    out["season_order"] = SEASON_ORDER
    return out
