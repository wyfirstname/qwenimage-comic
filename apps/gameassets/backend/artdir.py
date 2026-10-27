# -*- coding: utf-8 -*-
"""美术方向契约（Art Direction Contract）——借力 awesome-gamedev-agent-skills
的 create-game-assets 方法论，把「风格」从一句形容词升级成一份可编辑的契约。

来源拆解（github.com/gamedev-skills/awesome-gamedev-agent-skills）：
  该项目 76 个 SKILL.md 中与本流水线直接相关的是 disciplines/create-game-assets，
  它的核心主张：生图只是生产工具，产出是否可用由「契约 + 守门」决定：
    1. Lock the technical frame —— 锁技术框（引擎/画幅/透明/帧数）→ 本项目 presets/VIEW_LOCK
    2. Name the visual system   —— 命名视觉系统（八轴，本模块）
    3. Approve one visual target —— 先批一张基准图 → master 阶段
    4. Families not one-offs     —— 家族式生成，不变块复用 → 提示词组装
    5. Normalize deterministically —— 确定性后处理 → transmat/anim
    6. Validate in context       —— 场景内实测 → simulator/playtest
  本模块补齐第 2 条：八轴视觉系统 + 契约文本 + 提示词不变块。

为什么不用 LLM 推导：契约要求「稳定、可复现、用户可改」，规则推导
（风格基因 + 世界观关键词）结果确定性一致；用户在设置面板随时覆盖，
覆盖后以用户手填为准（derive 不再覆盖手填值）。
"""

from __future__ import annotations

# 八轴视觉系统（参考 skill 的 art-direction.md，翻译成生图提示词可消化的粒度）
AXES: dict[str, dict] = {
    "shape": {
        "name": "形状语言",
        "hint": "圆润/硬朗/块状/纤细？哪些形状表示友好、危险、稀有、可交互？",
        "derive": ("shape language: {shape_kw}, organic friendly silhouettes for allies, "
                   "spiky jagged silhouettes for monsters"),
    },
    "silhouette": {
        "name": "剪影要点",
        "name_en": "Silhouette",
        "hint": "游戏尺度下必须一眼认出什么？哪些部件不许和身体糊成一团？",
        "derive": "clean readable silhouette, key props clearly separated from body mass",
    },
    "value": {
        "name": "明暗结构",
        "hint": "几个明暗层次？灰度下是否还能和常见背景分开？",
        "derive": ("clear value structure, subject readable in grayscale "
                   "against gameplay backgrounds"),
    },
    "palette": {
        "name": "色彩角色",
        "hint": "强调色/危险色/奖励色/环境色/中立色分别是什么？",
        "derive": "consistent palette roles: unified environment tones, warm accent for rewards, cold desaturated tones for danger",
    },
    "edge": {
        "name": "边缘处理",
        "hint": "硬像素簇/干净矢量边/墨线/柔边？全套素材必须统一。",
        "derive": "uniform edge treatment across all assets, consistent outline weight",
    },
    "light": {
        "name": "光照方向",
        "hint": "光从哪来？软硬？有没有轮廓光？全套素材必须同向。",
        "derive": ("single consistent light direction across the whole asset family, "
                   "soft ambient occlusion, no per-asset light drift"),
    },
    "detail": {
        "name": "细节密度",
        "hint": "细节集中在哪里？游戏原生尺寸下哪些会消失？",
        "derive": ("detail concentrated on focal points (face, weapon head, roofline), "
                   "large flat areas stay clean at native game scale"),
    },
    "motion": {
        "name": "运动性格",
        "hint": "动画是利落/弹性/沉重/克制？允许多大形变？",
        "derive": "consistent motion character, stable proportions during walk cycles",
    },
}

AXIS_ORDER = list(AXES)
AXIS_IDS = set(AXIS_ORDER)

_AXIS_KW: dict[str, str] = {
    # 风格 → 形状语言的粗映射（规则推导用）
    "ghibli": "rounded organic curves",
    "pixel": "chunky blocky pixel clusters",
    "cyberpunk": "angular geometric panels",
    "lowpoly": "faceted planar geometry",
    "darkfantasy": "heavy ornate angular forms",
    "cel": "rounded bold cartoon shapes",
}

# 世界观关键词 → 主题修饰（规则推导，命中即拼）
_WORLD_KW: list[tuple[tuple[str, ...], str]] = [
    (("武侠", "仙侠", "修仙", "江湖"), "eastern fantasy motifs, flowing cloth and ribbon accents"),
    (("科幻", "太空", "星际", "机甲"), "sci-fi panel lines and emissive trims"),
    ((" medieval", "中世纪", "骑士", "城堡"), "medieval heraldic accents"),
    (("恐怖", "僵尸", "末日"), "weathered decay and desaturated danger tones"),
    (("萌", "Q版", "休闲", "农场"), "cute rounded proportions, soft candy accents"),
]


def derive(s: dict) -> dict:
    """规则推导：风格基因 + 世界观关键词 → 八轴契约 dict（axis → 英文句子）。
    只填空轴，不覆盖用户手填值。"""
    style = str(s.get("style") or "")
    text = f"{s.get('idea', '')} {s.get('desc', '')}".lower()
    out: dict[str, str] = {}
    for ax in AXIS_ORDER:
        spec = AXES[ax]
        if ax == "shape":
            kw = _AXIS_KW.get(style, "clean simple shapes")
            out[ax] = spec["derive"].format(shape_kw=kw)
            for keys, motif in _WORLD_KW:
                if any(k in text for k in keys):
                    out[ax] += f", {motif}"
                    break
        else:
            out[ax] = spec["derive"]
    return out


def normalize(artdir: dict | None) -> dict:
    """清洗用户输入：只留已知轴、剥空白、限长。空值轴剔除。"""
    out: dict[str, str] = {}
    for k, v in (artdir or {}).items():
        if k not in AXIS_IDS:
            continue
        v = str(v or "").strip()[:400]
        if v:
            out[k] = v
    return out


def prompt_block(artdir: dict | None) -> str:
    """契约 → 提示词不变块（ART DIRECTION 段）。空契约返回空串。"""
    parts = []
    for ax in AXIS_ORDER:
        v = (artdir or {}).get(ax)
        if v:
            parts.append(f"{ax}: {v}")
    return "; ".join(parts)


def is_hand(edited: bool) -> bool:
    return bool(edited)


def brief_md(s: dict) -> str:
    """契约 → art-direction-brief.md（导出 ZIP 内附，团队/引擎侧可读）。"""
    ad = s.get("artdir") or {}
    lines = [
        "# 美术方向契约（Art Direction Brief）",
        "",
        f"- 项目：{s.get('name', '')}",
        f"- 一句话：{s.get('idea', '')}",
        f"- 风格：{s.get('style_name') or s.get('style', '')}",
        f"- 时间背景：{s.get('time_name') or s.get('time_preset', '')}",
        f"- 视图锁：2.5D 45°（俯角 45°、正交等比、脚底贴地锚点）",
        f"- 契约来源：{'用户手填' if s.get('artdir_edited') else '规则自动推导'}",
        "",
        "| 轴 | 约定 |",
        "|----|------|",
    ]
    for ax in AXIS_ORDER:
        lines.append(f"| {AXES[ax]['name']} | {ad.get(ax, '（未约定）')} |")
    lines += [
        "",
        "> 本契约是家族一致性的锚：任何重跑/换装/候选都以它为不变块，",
        "> 漂移的素材按「回到基准图 + 受限编辑」处理，不要在漂移图上继续改。",
        "",
    ]
    return "\n".join(lines)
