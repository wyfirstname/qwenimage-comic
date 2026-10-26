# -*- coding: utf-8 -*-
"""时间背景预设：每个预设固定「色调 + 光照 + 负面词」，全素材集共用。

一致性第一层就在这里：同一素材集内，每张图（基准图、角色、道具、场景）
的提示词都强制拼上同一个 preset 的 tone 片段，负面词同样统一。
prompt 片段用英文（Qwen-Image 对英文色调词响应更稳），UI 展示用中文名。
"""

from __future__ import annotations

TIME_PRESETS: dict[str, dict] = {
    "dawn": {
        "id": "dawn",
        "name": "清晨",
        "hour": "06:00",
        "prompt": ("soft cool morning light, pale blue and misty pink tones, "
                    "gentle dawn glow on horizon, delicate long shadows, fresh airy atmosphere"),
        "negative": "harsh midday sun, dark night, saturated orange sunset",
        "swatch": ["#A8C8E8", "#D8C4E0", "#F2D5C4", "#88A8CC"],
    },
    "noon": {
        "id": "noon",
        "name": "正午",
        "hour": "12:00",
        "prompt": ("bright neutral daylight, clear blue sky, strong overhead sunlight, "
                    "crisp high-contrast shadows, vivid saturated colors"),
        "negative": "night, sunset colors, dim moody lighting, fog",
        "swatch": ["#5AA7E8", "#8FD0F5", "#F5F0DC", "#3B7BD8"],
    },
    "dusk": {
        "id": "dusk",
        "name": "黄昏",
        "hour": "18:00",
        "prompt": ("warm orange and magenta sunset glow, golden rim light, "
                    "long soft shadows, amber highlights fading into violet dusk"),
        "negative": "harsh midday sun, cold blue moonlight, overcast grey sky",
        "swatch": ["#F08300", "#E85A7B", "#8E5AA8", "#F5B041"],
    },
    "night": {
        "id": "night",
        "name": "夜晚",
        "hour": "22:00",
        # 注意：旧措辞 "deep blue moonlight, cool desaturated shadows, strong chiaroscuro,
        # vast cold darkness" 会把 2.1 蒸馏模型（CFG=1、8步）推进采样崩溃，整图烧成
        # 近纯黑（实测 2026-10-25，多组对照实验定位）。改用温和夜色词。
        "prompt": ("night, bright moonlight, deep blue night sky, "
                    "warm lantern glow, gentle contrast, details visible in shadows"),
        "negative": "bright daylight, clear blue sky, warm sunset tones",
        "swatch": ["#1F2E5C", "#2851A3", "#565B63", "#F5B041"],
    },
    "overcast": {
        "id": "overcast",
        "name": "阴天",
        "hour": "全天",
        "prompt": ("soft diffused overcast light, muted grey-blue palette, "
                    "low contrast, gentle fog, no visible sun, quiet melancholic mood"),
        "negative": "strong direct sunlight, vivid saturated colors, blue sky",
        "swatch": ["#8C9199", "#A8B8C4", "#C3B091", "#6B7A8A"],
    },
    "snow": {
        "id": "snow",
        "name": "雪天",
        "hour": "全天",
        "prompt": ("cold snowy daylight, white and pale blue palette, "
                    "soft bounced light from snow, breath-fog atmosphere, serene winter mood"),
        "negative": "warm summer colors, green foliage, desert, sunset glow",
        "swatch": ["#E8EEF5", "#C9D8E8", "#9FB8D0", "#DDE5EE"],
    },
}


def get_preset(preset_id: str) -> dict:
    if preset_id not in TIME_PRESETS:
        raise KeyError(f"未知时间背景：{preset_id}（可选：{', '.join(TIME_PRESETS)}）")
    return TIME_PRESETS[preset_id]


# ------------------------------------------------------------
#  四季变体：场景在同一时间背景（光照色温）下换季。
#  季节改变的是内容（植被/雪/花）与部分环境色，不改变色温与光源方向，
#  所以季节卡的色调校验阈值在流水线里放宽 1.6 倍（合法偏色，不是跑色）。
# ------------------------------------------------------------
SEASONS: dict[str, dict] = {
    "spring": {"name": "春", "prompt": ("fresh spring atmosphere, tender green foliage, "
                                        "pink blossoms drifting, soft rain-washed light, new growth")},
    "summer": {"name": "夏", "prompt": ("lush summer atmosphere, deep green foliage, "
                                        "vivid flowers, bright warm sunlight, clear air")},
    "autumn": {"name": "秋", "prompt": ("autumn atmosphere, golden and crimson foliage, "
                                        "harvest light, falling leaves, mellow warm haze")},
    "winter": {"name": "冬", "prompt": ("winter atmosphere, thick snow cover, bare branches, "
                                        "icicles, cold pale light, breath fog")},
}
SEASON_ORDER = ["spring", "summer", "autumn", "winter"]


# ------------------------------------------------------------
#  性别变体：主角默认男女各一版（同一名字展开成两条）。
#  性别词进提示词前半段——2.1 对前置主体词最敏感，写在描述后面容易被忽略。
# ------------------------------------------------------------
GENDERS: dict[str, dict] = {
    "male": {"name": "男", "prompt": ("male character, masculine build, "
                                      "male facial features, male body proportions")},
    "female": {"name": "女", "prompt": ("female character, feminine build, "
                                        "female facial features, female body proportions")},
}
GENDER_ORDER = ["male", "female"]


def entry_genders(entry: dict) -> list[str]:
    """方案条目 → 要展开的性别列表。

    显式 genders 优先；否则按名字猜：含「女主角」→ 只女；含「主角」→ 男女各一；
    其余（NPC/导师/商人）默认男，用户可在方案编辑里补勾。
    """
    raw = entry.get("genders")
    if raw:
        picked = [g for g in GENDER_ORDER if g in raw]
        return picked or ["male"]
    name = str(entry.get("name") or "")
    if "女主" in name:
        return ["female"]
    if entry.get("protagonist") or "主角" in name:
        return ["male", "female"]
    return ["male"]



# ------------------------------------------------------------
#  武器等级：固定四级，等级词直接描述材质与光效（模型可画）。
# ------------------------------------------------------------
WEAPON_TIERS: dict[str, dict] = {
    "common":    {"name": "普通", "prompt": ("simple humble equipment, rough wood and iron, "
                                             "visible wear, no glow, practical design")},
    "fine":      {"name": "精良", "prompt": ("well-forged steel equipment, fine craftsmanship, "
                                             "clean polish, subtle engraved pattern")},
    "epic":      {"name": "史诗", "prompt": ("enchanted equipment, glowing runes carved along the blade, "
                                             "ornate metalwork, faint magical aura")},
    "legendary": {"name": "传说", "prompt": ("legendary artifact, radiant divine glow, elaborate "
                                             "gold filigree, gemstones, awe-inspiring presence")},
}
TIER_ORDER = ["common", "fine", "epic", "legendary"]


# ------------------------------------------------------------
#  怪物等级：不再是一锅「野怪」，按战斗定位分四级。
#  等级同时决定画幅——体积感是等级的主要视觉信号，杂兵用 640 省显存，
#  终局 1024 顶到 6GB 卡能稳出的上限。size 后面的 note 就是「为什么这么大」。
# ------------------------------------------------------------
MONSTER_TIERS: dict[str, dict] = {
    "minion": {
        "name": "杂兵", "size": (640, 640),
        "prompt": ("small minion creature, low threat level, simple readable silhouette, "
                   "hunched scavenger proportions, single accent color, plain hide"),
        "note": "成群刷的杂兵，640 见方足够又省显存；轮廓要一眼能认，细节多了反而糊",
    },
    "elite": {
        "name": "精英", "size": (768, 768),
        "prompt": ("elite variant creature, heavier armored hide, old battle scars, "
                   "glowing eyes, clearly larger and meaner than a common one"),
        "note": "带甲带光效的精英，768 才留得住材质与伤疤细节",
    },
    "boss": {
        "name": "首领", "size": (896, 896),
        "prompt": ("boss-tier behemoth, towering imposing scale, heavy horned silhouette, "
                   "dramatic menacing presence, powerful planted stance"),
        "note": "单个 BOSS 要撑满屏幕，896 保证体积感不被压糊",
    },
    "worldboss": {
        "name": "终局", "size": (1024, 1024),
        "prompt": ("world-ending colossus, colossal otherworldly anatomy, cataclysmic aura, "
                   "silhouette dwarfs the frame, epic apocalyptic scale"),
        "note": "终局演出用，1024 是 2060 6GB 能稳定出图的上限，再大就开始崩结构",
    },
}
MONSTER_TIER_ORDER = ["minion", "elite", "boss", "worldboss"]


def monster_tier(entry: dict) -> str:
    """方案条目的怪物等级 id（老数据没有就按杂兵）。"""
    tid = str(entry.get("tier_id") or "").strip()
    return tid if tid in MONSTER_TIERS else "minion"


# ------------------------------------------------------------
#  场景用途：每条场景必须显式选一个用途，用途决定画幅。
#  以前「场景统一 1152×640」是拍脑袋——平铺地表和竖屏副本根本不该同尺寸。
#  reason 会直接显示在 UI 上，让「为什么这么大」有据可查。
# ------------------------------------------------------------
SCENE_USAGES: dict[str, dict] = {
    "battle": {
        "name": "横版战斗背景", "size": (1152, 640), "aspect": "16:9",
        "reason": ("对齐全高清 16:9，等比缩到 1920×1080 不裁切；主体压中央 70% 安全区，"
                   "左右各留 15% 作视差滚动带，站中轴的角色不会被切到"),
    },
    "tile": {
        "name": "可平铺地表", "size": (1024, 1024), "aspect": "1:1",
        "reason": ("四边无缝的方形贴图，按 512 网格切片进 TileMap 无限拼接；"
                   "横竖等长保证任意方向平铺都不拉伸"),
    },
    "portrait": {
        "name": "竖屏副本", "size": (768, 1152), "aspect": "2:3",
        "reason": ("手机竖屏 9:16 的中间安全区，满屏显示不裁切；上下各留一段给状态栏与对话框"),
    },
    "banner": {
        "name": "章节横幅", "size": (1152, 512), "aspect": "9:4",
        "reason": ("加载页 / 章节标题的超宽横幅，故意压扁高度让天际线成主导，左右留白写标题"),
    },
    "iso": {
        "name": "等距斜视小场景", "size": (896, 896), "aspect": "1:1",
        "reason": ("战棋 / 等距副本的一整块战场：7×7 格 × 128px 正好铺满，"
                   "方图便于相机 45° 环绕不露边"),
    },
}
SCENE_USAGE_ORDER = ["battle", "tile", "portrait", "banner", "iso"]


def scene_usage(entry: dict) -> str:
    """方案条目的场景用途 id。老数据没有 usage 就按 tileable 猜。"""
    uid = str(entry.get("usage") or "").strip()
    if uid in SCENE_USAGES:
        return uid
    return "tile" if entry.get("tileable") else "battle"


def scene_size(entry: dict) -> tuple[int, int]:
    return SCENE_USAGES[scene_usage(entry)]["size"]


# ------------------------------------------------------------
#  方案 → 阶段条目表（store 归一化与 pipeline 执行共用；场景全部 ×4 季）
#  条目带稳定 id（itm_*）：方案编辑后靠 id 对账，没改动的条目不重跑。
# ------------------------------------------------------------
PLAN_KEYS = {"scene": "scenes", "character": "characters", "pet": "pets",
             "weapon": "weapons", "building": "buildings", "monster": "monsters"}
ASSET_KINDS = ("scene", "character", "pet", "weapon", "building", "monster")


def build_stage_items(plan: dict, existing: dict[str, list[dict]] | None = None
                      ) -> dict[str, list[dict]]:
    """方案展开为条目表。existing 提供旧条目（按 key 对账沿用 id/状态）。

    条目 key：非场景 = 方案条目在 PLAN_KEYS 里的下标；场景 = 下标+季节。
    """
    from shared.models_util import new_id
    existing = existing or {}
    out: dict[str, list[dict]] = {}
    for kind in ASSET_KINDS:
        old_items = existing.get(kind, [])
        old_by_key = {}
        for it in old_items:
            old_by_key.setdefault(it.get("key") or it.get("id"), it)
        items: list[dict] = []
        for idx, entry in enumerate(plan[PLAN_KEYS[kind]]):
            def _inherit(old, name, desc, **meta):
                """旧条目内容没变才沿用状态；任一字段变了回到 pending（触发重跑）。"""
                if not old or old.get("name") != name or old.get("desc") != desc:
                    return "pending"
                for k, v in meta.items():
                    if (old.get(k) or "") != (v or ""):
                        return "pending"
                return old.get("status", "pending")
            if kind == "scene":
                usage = scene_usage(entry)
                seasons = entry.get("seasons") or SEASON_ORDER
                for season in seasons:
                    key = f"{idx}:{season}"
                    old = old_by_key.get(key) or old_by_key.get(f"{entry['name']}:{season}")
                    items.append({
                        "id": (old or {}).get("id") or new_id("itm"),
                        "key": key,
                        "name": entry["name"], "desc": entry["desc"],
                        "season": season,
                        "usage": usage, "usage_name": SCENE_USAGES[usage]["name"],
                        "tileable": usage == "tile",
                        "status": _inherit(old, entry["name"], entry["desc"],
                                           usage=usage),
                    })
            elif kind == "character":
                gids = entry_genders(entry)
                for gid in gids:
                    key = f"{idx}:{gid}"
                    old = (old_by_key.get(key)
                           or old_by_key.get(f"{entry['name']}:{gid}")
                           or (old_by_key.get(entry["name"]) if len(gids) == 1 else None))
                    items.append({
                        "id": (old or {}).get("id") or new_id("itm"),
                        "key": key,
                        "name": entry["name"], "desc": entry["desc"],
                        "gender": gid,
                        "status": _inherit(old, entry["name"], entry["desc"],
                                           gender=gid),
                    })
            else:
                key = str(idx)
                old = old_by_key.get(key) or old_by_key.get(entry["name"])
                item = {"id": (old or {}).get("id") or new_id("itm"),
                        "key": key,
                        "name": entry["name"], "desc": entry["desc"]}
                if kind == "monster":
                    tid = monster_tier(entry)
                    item.update({"tier_id": tid,
                                 "tier": MONSTER_TIERS[tid]["name"],
                                 "scene": entry.get("scene")})
                else:
                    item.update({"tier": entry.get("tier"),
                                 "tier_id": entry.get("tier_id")})
                item["status"] = _inherit(old, entry["name"], entry["desc"],
                                          tier_id=item.get("tier_id"),
                                          scene=item.get("scene"))
                items.append(item)
        out[kind] = items
    return out


# ------------------------------------------------------------
#  按类画幅：640~1152 长边、64 对齐（ComfyUI latent 对齐要求）。
#  场景/怪物不用这里的默认值——它们的画幅由「场景用途 / 怪物等级」逐条决定，
#  见 SCENE_USAGES / MONSTER_TIERS 里的 size + reason。
#  set["sizes"][kind] 只对没有逐条设计的类（character/pet/weapon/building）生效。
# ------------------------------------------------------------
ASPECTS: dict[str, tuple[int, int]] = {
    "scene":     (1152, 640),   # 16:9 横幅概念图（= battle 用途，仅兜底）
    "character": (512, 768),    # 2:3 立绘
    "pet":       (768, 768),    # 1:1
    "weapon":    (768, 768),    # 1:1 图标
    "building":  (896, 672),    # 4:3
    "monster":   (640, 640),    # 1:1（= 杂兵，仅兜底）
}
_MASTER_SIZE = (1152, 640)


def _align(size) -> tuple[int, int]:
    w, h = int(size[0]), int(size[1])
    return max(256, w - w % 64), max(256, h - h % 64)


def asset_size(s: dict, kind: str, item: dict | None = None) -> tuple[int, int]:
    """画幅：本条设计（场景用途 / 怪物等级）> 项目按类覆盖 > 类默认。"""
    if item:
        if kind == "scene":
            return _align(scene_size(item))
        if kind == "monster":
            return _align(MONSTER_TIERS[monster_tier(item)]["size"])
    size = ((s.get("sizes") or {}).get(kind)) or ASPECTS.get(kind, (768, 768))
    return _align(size)


# ------------------------------------------------------------
#  透明底：按类指定抠图方式。none=不抠（场景/建筑默认）；
#  rembg=AI 抠图（U2Net，白袍白底这类色键必败的图也能抠，默认）；
#  chroma=白底色键（快，但主体本身接近白色时会抠穿/抠不动）。
#  set["transparent"] 覆盖。
# ------------------------------------------------------------
TRANSPARENT_DEFAULT: dict[str, str] = {
    "scene": "none", "building": "none",
    "character": "rembg", "pet": "rembg",
    "weapon": "rembg", "monster": "rembg",
}


def transparent_mode(s: dict, kind: str) -> str:
    return ((s.get("transparent") or {}).get(kind)) or TRANSPARENT_DEFAULT.get(kind, "none")


# ------------------------------------------------------------
#  按类提示词：固定模板（代码里，不可改）+ 项目级补充（可改）。
#  补充词只追加在这类素材的提示词尾部、负面前部，不影响其它类。
#  set["prompts"][kind] = {"extra": "...", "negative": "..."}
# ------------------------------------------------------------
CLASS_LABELS: dict[str, str] = {
    "scene": "场景", "character": "角色", "pet": "宠物",
    "weapon": "武器", "building": "建筑", "monster": "野怪",
}

# 给 UI 显示的固定模板摘要（真正的拼装在 pipeline.asset_prompt 里）
CLASS_TEMPLATE_HINT: dict[str, str] = {
    "scene": ("环境概念图 · 大全景 · 无角色无文字 · 画幅由「用途」定："
              "16:9 战斗 / 1:1 平铺 / 2:3 竖屏 / 9:4 横幅 / 1:1 等距战场"),
    "character": "全身立绘 · 单角色 · 正面站姿 · 居中 · 先写性别再写外观",
    "pet": "宠物伙伴设计 · 全身 · 侧前 45° · 居中",
    "weapon": "武器设计 · 单件 · 四分之三视角 · 居中 · 可叠加等级词",
    "building": "建筑外观概念图 · 完整结构可见 · 无内部视角",
    "monster": ("怪物设计 · 全身 · 居中 · 画幅由「等级」定："
                "杂兵 640 / 精英 768 / 首领 896 / 终局 1024，并写明出没场景"),
}


def prompt_extra(s: dict, kind: str) -> str:
    """该类追加的正向提示词（项目级，可空）。"""
    return str(((s.get("prompts") or {}).get(kind) or {}).get("extra") or "").strip()


def prompt_negative(s: dict, kind: str) -> str:
    """该类追加的负面词（项目级，可空）。"""
    return str(((s.get("prompts") or {}).get(kind) or {}).get("negative") or "").strip()
