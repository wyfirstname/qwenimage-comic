# -*- coding: utf-8 -*-
"""分镜节奏模板（handraw SB-71 的**轻量**用法）。

先分清两件完全不同的事，否则一定做错：

| | 本项目的 layout | handraw 的 SB-xxx |
|---|---|---|
| 作用阶段 | **排版**（PIL 切格拼页） | **生成**（写给模型的版面语言） |
| 内容 | 一页切几格（1×2 / 2×2） | 阅读顺序、大小格、破框、正反打、特写链 |
| 前提 | 逐格出图，后拼 | 模型一次画一整页 4~9 格漫画 |

本项目走的是**逐格出图**通路，所以 SB 不能照搬成"整页直出"，
而是当**分镜节奏模板**用：选一个编号，只取它的**节奏语义**
（这一页几格、大小格分布、镜头序列），出图方式一行不改。

⚠️ SB-71 里有一批（跨格人物 / 跨格背景 / 破框 / 一镜到底）**强依赖整页画布**，
   逐格通路里只能降级成"景别 + 构图"近似，`degraded=True` 标注，界面上提示"降级可用"。

节奏家族：把 71 个编号按节奏语义归到若干"家族"，每个家族给一套
   镜头序列 + 推荐格数。这样既保留编号可复现，又不必逐个手写 71 套。
"""

from __future__ import annotations

from typing import Any, Optional

from app.style_library import library as style_library

# 节奏家族定义：key → {name, shots(景别循环), cells(推荐每页格数), layout(推荐切格), note}
_FAMILIES: dict[str, dict[str, Any]] = {
    "steady": {
        "name": "稳定叙事",
        "desc": "等分格、平稳推进，适合日常对白",
        "shots": ["medium", "close", "medium", "over"],
    },
    "hero": {
        "name": "主次强调",
        "desc": "一大配多小，突出某个视觉重点",
        "shots": ["wide", "close", "medium", "close"],
    },
    "escalate": {
        "name": "层层逼近",
        "desc": "由远到近 / 特写链，紧张感累积",
        "shots": ["wide", "medium", "close", "extreme"],
    },
    "reveal": {
        "name": "揭晓反差",
        "desc": "由近到远，先特写后拉远揭晓环境",
        "shots": ["extreme", "close", "medium", "wide"],
    },
    "action": {
        "name": "动作爆发",
        "desc": "分解动作 / 残影，强动势",
        "shots": ["full", "action", "close", "action"],
    },
    "duel": {
        "name": "对峙博弈",
        "desc": "正反打 / 左右对峙，交替对视",
        "shots": ["medium", "over", "close", "over"],
    },
    "montage": {
        "name": "蒙太奇",
        "desc": "碎片 / 交叉剪辑，跳跃推进",
        "shots": ["close", "full", "extreme", "wide"],
    },
    "quiet": {
        "name": "留白余韵",
        "desc": "急停 / 留白，反高潮",
        "shots": ["wide", "wide", "medium", "wide"],
    },
    "vista": {
        "name": "宏大场景",
        "desc": "宽银幕 / 满版，建立场景",
        "shots": ["wide", "wide", "full", "wide"],
    },
    "life": {
        "name": "生活条漫",
        "desc": "竖版等分生活分镜，上图下文",
        "shots": ["full", "medium", "close", "medium"],
    },
}

# SB 编号 → 家族 + 推荐格数。逐条对着 handraw 的 keywords 归的类。
_SB_MAP: dict[str, dict[str, Any]] = {
    "SB-001": {"family": "steady", "cells": 4},
    "SB-002": {"family": "steady", "cells": 4},
    "SB-003": {"family": "hero", "cells": 5},
    "SB-004": {"family": "hero", "cells": 5},
    "SB-005": {"family": "hero", "cells": 5},
    "SB-006": {"family": "hero", "cells": 5},
    "SB-007": {"family": "action", "cells": 4, "degraded": True},
    "SB-008": {"family": "steady", "cells": 4},
    "SB-009": {"family": "steady", "cells": 4},
    "SB-010": {"family": "escalate", "cells": 5, "degraded": True},
    "SB-011": {"family": "hero", "cells": 5, "degraded": True},
    "SB-012": {"family": "montage", "cells": 6},
    "SB-013": {"family": "action", "cells": 4, "degraded": True},
    "SB-014": {"family": "action", "cells": 4, "degraded": True},
    "SB-015": {"family": "vista", "cells": 3, "degraded": True},
    "SB-016": {"family": "vista", "cells": 3, "degraded": True},
    "SB-017": {"family": "montage", "cells": 6},
    "SB-018": {"family": "vista", "cells": 4},
    "SB-019": {"family": "escalate", "cells": 4, "degraded": True},
    "SB-020": {"family": "escalate", "cells": 6},
    "SB-021": {"family": "escalate", "cells": 4},
    "SB-022": {"family": "escalate", "cells": 4},
    "SB-023": {"family": "escalate", "cells": 4},
    "SB-024": {"family": "reveal", "cells": 4},
    "SB-025": {"family": "duel", "cells": 4},
    "SB-026": {"family": "escalate", "cells": 3},
    "SB-027": {"family": "action", "cells": 4},
    "SB-028": {"family": "action", "cells": 4},
    "SB-029": {"family": "quiet", "cells": 4},
    "SB-030": {"family": "quiet", "cells": 4},
    "SB-031": {"family": "quiet", "cells": 3},
    "SB-032": {"family": "duel", "cells": 2},
    "SB-033": {"family": "duel", "cells": 2},
    "SB-034": {"family": "montage", "cells": 4},
    "SB-035": {"family": "montage", "cells": 4},
    "SB-036": {"family": "montage", "cells": 4, "degraded": True},
    "SB-037": {"family": "montage", "cells": 4, "degraded": True},
    "SB-038": {"family": "montage", "cells": 6},
    "SB-039": {"family": "montage", "cells": 4},
    "SB-040": {"family": "montage", "cells": 6},
    "SB-041": {"family": "vista", "cells": 3},
    "SB-042": {"family": "vista", "cells": 4},
    "SB-043": {"family": "vista", "cells": 4},
    "SB-044": {"family": "montage", "cells": 4},
    "SB-045": {"family": "montage", "cells": 5},
    "SB-046": {"family": "hero", "cells": 5},
    "SB-047": {"family": "hero", "cells": 5},
    "SB-048": {"family": "quiet", "cells": 4, "degraded": True},
    "SB-049": {"family": "escalate", "cells": 4, "degraded": True},
    "SB-050": {"family": "escalate", "cells": 4},
    "SB-051": {"family": "quiet", "cells": 4},
    "SB-052": {"family": "quiet", "cells": 4, "degraded": True},
    "SB-053": {"family": "quiet", "cells": 4, "degraded": True},
    "SB-054": {"family": "quiet", "cells": 3},
    "SB-055": {"family": "hero", "cells": 2},
    "SB-056": {"family": "vista", "cells": 2, "degraded": True},
    "SB-057": {"family": "hero", "cells": 3},
    "SB-058": {"family": "vista", "cells": 3},
    "SB-059": {"family": "steady", "cells": 4},
    "SB-060": {"family": "vista", "cells": 3, "degraded": True},
    "SB-061": {"family": "vista", "cells": 3, "degraded": True},
    "SB-062": {"family": "duel", "cells": 2},
    "SB-063": {"family": "quiet", "cells": 4},
    "SB-064": {"family": "quiet", "cells": 4},
    "SB-065": {"family": "montage", "cells": 4},
    "SB-066": {"family": "action", "cells": 3},
    "SB-067": {"family": "quiet", "cells": 4},
    "SB-068": {"family": "montage", "cells": 6},
    "SB-069": {"family": "life", "cells": 2},
    "SB-070": {"family": "life", "cells": 4},
    "SB-071": {"family": "life", "cells": 3},
}


def _sb_meta(sb_id: str) -> Optional[dict]:
    lib = style_library()
    for x in lib.sb_templates():
        if x.get("id") == sb_id:
            return x
    return None


def build(sb_id: str | None) -> Optional[dict]:
    """把 SB 编号展开成节奏口径。

    返回 None 表示未指定（调用方沿用现有 _AUTO_SHOT_CYCLE 与项目排版预设）。
    否则返回：
      id / name / name_en / family / family_name / desc
      shots        —— 这一页的镜头序列（循环取用）
      cells        —— 推荐每页格数
      degraded     —— 是否强依赖整页画布（逐格通路里只能近似）
      keywords     —— handraw 原始关键词（界面展示用）
    """
    if not sb_id:
        return None
    meta = _sb_meta(sb_id)
    plan = _SB_MAP.get(sb_id)
    if not meta or not plan:
        return None
    fam = _FAMILIES.get(plan["family"], _FAMILIES["steady"])
    return {
        "id": sb_id,
        "name": meta.get("name") or "",
        "name_en": meta.get("name_en") or "",
        "keywords": meta.get("keywords") or [],
        "family": plan["family"],
        "family_name": fam["name"],
        "desc": fam["desc"],
        "shots": list(fam["shots"]),
        "cells": int(plan.get("cells") or len(fam["shots"])),
        "degraded": bool(plan.get("degraded")),
    }


def apply_to_panels(project: dict, panels: list[dict],
                    replace_shot: bool = True) -> list[dict]:
    """按节奏模板给分镜重排镜头序列（**逐格通路**下的轻量应用）。

    只改 shot（景别），不动 scene/dialogue；分页交给调用方的 renumber。
    replace_shot=False 时保留用户已显式设置的景别。
    """
    rhythm = build(project.get("rhythm_template"))
    if not rhythm:
        return panels
    shots = rhythm["shots"] or ["medium"]
    for i, p in enumerate(panels):
        if replace_shot or not p.get("shot"):
            p["shot"] = shots[i % len(shots)]
    return panels


def shot_cycle(sb_id: str | None) -> list[str]:
    """该节奏模板的镜头循环；未指定时返回空列表。"""
    r = build(sb_id)
    return list(r["shots"]) if r else []


def list_families() -> list[dict]:
    return [{"key": k, **v} for k, v in _FAMILIES.items()]
