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
#  方案 → 阶段条目表（store 归一化与 pipeline 执行共用；场景全部 ×4 季）
# ------------------------------------------------------------
PLAN_KEYS = {"scene": "scenes", "character": "characters", "pet": "pets",
             "weapon": "weapons", "building": "buildings", "monster": "monsters"}
ASSET_KINDS = ("scene", "character", "pet", "weapon", "building", "monster")


def build_stage_items(plan: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for kind in ASSET_KINDS:
        items: list[dict] = []
        for entry in plan[PLAN_KEYS[kind]]:
            if kind == "scene":
                for season in SEASON_ORDER:
                    items.append({"name": entry["name"], "desc": entry["desc"],
                                  "season": season, "status": "pending"})
            else:
                items.append({"name": entry["name"], "desc": entry["desc"],
                              "tier": entry.get("tier"), "tier_id": entry.get("tier_id"),
                              "status": "pending"})
        out[kind] = items
    return out
