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
        "prompt": ("deep blue moonlight, cool desaturated shadows, strong chiaroscuro, "
                    "small warm lantern accents against vast cold darkness"),
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
