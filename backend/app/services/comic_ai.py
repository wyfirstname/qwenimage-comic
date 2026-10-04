# -*- coding: utf-8 -*-
"""漫画工作室的本地 AI 能力（剧本 / 角色提取 / 场景提取 / 智能分镜）。

全部走本机推理：文本用 Qwen-Image 自带的 Qwen3-VL-8B（ComfyUI TextGenerate 节点），
出图用本机 Qwen-Image GGUF。不联网、不依赖云端 API、不需要视频模型。

提示词模板参考「短剧创作」项目的成熟做法（剧本 → 人物设计 → 场景设计 → 分镜拆解），
并按漫画生产做了两点改造：
  1. 景别词表与站内一致（远景/全景/中景/近景/特写/动作/过肩/背影），
     保证 LLM 产出的剧本能被规则解析器无损拆格；
  2. 角色 / 场景都额外产出一条英文绘图提示词，直接喂给本地出图模型。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

from app import comic_repo as crepo
from app.inference.comfy_text import LocalTextError, text_engine
from app.services.comic import LAYOUT_PRESETS, SHOT_PRESETS, STYLE_PRESETS, parse_script

ProgressCb = Optional[Callable[[float], None]]

# 站内景别词表（LLM 必须从中选择，否则拆格时会退化成普通文字）
SHOT_NAMES = "、".join(v["name"] for v in SHOT_PRESETS.values())
STYLE_NAMES = "、".join(v["name"] for v in STYLE_PRESETS.values())

# ------------------------------------------------------------
#  JSON 键名兼容
# ------------------------------------------------------------
# 本机 8B 模型经常不听指令，把 "dialogue" 写成「对白」「台词」，把 "name" 写成
# 「姓名」…… 早期版本只认英文字段名，结果整格对白被静默丢弃（用户报的「分镜里
# 没有对白」就是这个）。这里做一层别名映射，中英都收。
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    # 分镜
    "page": ("page", "页码", "页", "页数", "第几页"),
    "shot": ("shot", "景别", "镜头", "景别类型"),
    "scene_ref": ("scene_ref", "scene_name", "场景", "场景名", "所属场景", "场景引用"),
    "scene": ("scene", "画面", "画面描述", "描述", "场景描述", "action", "content"),
    "dialogue": ("dialogue", "对白", "台词", "对话", "旁白", "说话", "对白内容"),
    "sfx": ("sfx", "音效", "拟声词", "拟声", "特效音"),
    # 角色
    "name": ("name", "姓名", "角色名", "人物", "人物名", "名称"),
    "role": ("role", "角色定位", "定位", "身份", "类型"),
    "gender": ("gender", "性别", "sex"),
    "age": ("age", "年龄", "年纪"),
    "personality": ("personality", "性格", "个性"),
    "appearance": ("appearance", "外貌", "长相", "外形", "容貌"),
    "outfit": ("outfit", "服装", "衣着", "服饰", "穿着"),
    "background": ("background", "背景", "小传", "人物小传", "简介"),
    "detail_prompt": ("detail_prompt", "英文描述", "绘图描述", "prompt", "英文提示词"),
    # 场景
    "location": ("location", "地点", "具体地点", "位置"),
    "time_of_day": ("time_of_day", "时间", "时段", "时间点"),
    "atmosphere": ("atmosphere", "氛围", "气氛", "基调"),
    "desc": ("desc", "description", "描述", "画面描述", "说明"),
    "prompt": ("prompt", "英文提示词", "英文描述", "绘图提示词", "提示词"),
}


def _pick(item: dict, key: str, default: Any = "") -> Any:
    """按别名表从模型返回的对象里取值（中英键名都认）。"""
    if not isinstance(item, dict):
        return default
    for k in _FIELD_ALIASES.get(key, (key,)):
        if k in item:
            v = item[k]
            if v is None:
                continue
            if isinstance(v, str) and not v.strip():
                continue
            return v
    return default


def _as_text(item: dict, key: str, default: str = "") -> str:
    v = _pick(item, key, default)
    if isinstance(v, (list, tuple)):
        return " / ".join(str(x).strip() for x in v if str(x).strip())
    return str(v).strip() if v is not None else default


# 形如 「台词」 “台词” 『台词』 的引用块（模型常把对白混在画面描述里）
_QUOTE_RE = re.compile(r"[「『“\"]([^」』”\"]{1,80})[」』”\"]")


def _style_name(project: dict) -> str:
    p = STYLE_PRESETS.get(project.get("style") or "", {})
    return p.get("name") or "日式黑白漫画"


def _cast_block(project: dict) -> str:
    """已提取的角色设定，注入后续提示词以保持前后一致。"""
    chars = crepo.list_characters(project["id"])
    if not chars:
        return ""
    lines = []
    for c in chars:
        bits = [c.get("name") or "", f"（{c.get('role') or '配角'}"]
        for key in ("gender", "age"):
            if c.get(key):
                bits.append(f"，{c[key]}")
        bits.append("）")
        detail = "；".join(x for x in (c.get("personality"), c.get("appearance"), c.get("outfit")) if x)
        lines.append(f"- {''.join(bits)}：{detail}" if detail else f"- {''.join(bits)}")
    return "本作人物设定（画面中出现这些人物时，必须写完整姓名，禁止用「男子/女孩」等泛称）：\n" \
        + "\n".join(lines) + "\n"


def _scene_block(project: dict) -> str:
    scenes = crepo.list_scenes(project["id"])
    if not scenes:
        return ""
    lines = []
    for s in scenes:
        tail = "，".join(x for x in (s.get("location"), s.get("time_of_day"), s.get("atmosphere")) if x)
        lines.append(f"- {s.get('name') or ''}：{tail}" if tail else f"- {s.get('name') or ''}")
    return "本作场景设定（分镜的 scene 字段请从下列场景名中选择）：\n" + "\n".join(lines) + "\n"


# ============================================================
#  一、剧本创作
# ============================================================

SCRIPT_SYSTEM = (
    "你是一位资深的漫画编剧，擅长把梗概扩写成节奏紧凑、画面感强的分格剧本。"
    "你输出的内容会被自动拆成漫画格子，因此格式必须严格规范。"
)

SCRIPT_USER_TMPL = """请为下面的漫画作品创作剧本正文。

作品名：{title}
画风：{style}
本次任务：写第 {start_page} 页到第 {end_page} 页，共 {pages} 页（每页 4 格，共 {cells} 格）
故事梗概：{idea}
{cast_block}{scene_block}{previous}
格式要求（严格遵守，输出会被程序解析）：
1. 用「第 N 页」单独一行分页，页码从 {start_page} 开始连续编号，每页正好 4 格；
2. 每一格写一行，行首用 [景别] 开头；景别只能从这 {shot_count} 个词里选：{shots}
3. 有台词就在同一行用「」包起来；没有台词的行不要出现「」；
4. 需要拟声词时，在描述后面写 [音效：轰隆] 这样的标记；
5. 不要写编号、不要写"第X格"、不要用 Markdown 标记、不要输出任何解释或标题行；
6. 画面描述要具体可画：谁、在哪、在做什么、什么表情，25 字以内，不要写心理活动。

只输出剧本正文（第 {start_page} 页到第 {end_page} 页），不要重述前文。
"""

# 单次生成的最大页数（再长就分幕多次生成，见 generate_script）
MAX_SCRIPT_PAGES = 20
# 每一幕的页数：本机 CPU 文本推理很慢，一次要太多容易被模型截断/跑偏
SCRIPT_CHUNK_PAGES = 4


def _count_pages(text: str) -> int:
    """粗算剧本已有多少页（用于续写时决定从第几页开始）。"""
    n = len(re.findall(r"第\s*[0-9一二三四五六七八九十]+\s*页", text or ""))
    if n:
        return n
    lines = [l for l in (text or "").splitlines() if l.strip()]
    return max(0, (len(lines) + 3) // 4)


def generate_script(project: dict, *, pages: int = 2, idea: str = "", mode: str = "new",
                    temperature: float = 0.85, on_progress: ProgressCb = None,
                    note: Optional[Callable[[str], None]] = None) -> dict:
    """按梗概生成剧本正文（写回项目的 synopsis 字段）。

    pages 最长 20 页；超过一幕（4 页）时**分幕多次调用**并把前文作为上下文，
    这样既不会超过小模型的输出上限，也不会写到一半被截断（用户反馈的
    「一个故事都生成不全」就是这个原因）。

    mode="continue" 时在现有剧本后面接着写，用于把短故事续成长篇。
    """
    pages = max(1, min(int(pages or 1), MAX_SCRIPT_PAGES))
    base_text = ""
    if mode == "continue":
        base_text = (project.get("synopsis") or "").strip()
        if not base_text:
            raise LocalTextError("还没有剧本可以续写，请先生成第一幕。")

    idea_text = (idea or "").strip()
    if not idea_text and mode != "continue":
        idea_text = "（未提供梗概，请自行构思一个有冲突、有悬念的故事）"
    elif not idea_text:
        idea_text = "（延续既有故事，不要另起炉灶）"

    cast, scenes = _cast_block(project), _scene_block(project)
    done_pages = _count_pages(base_text) if base_text else 0
    chunks: list[str] = []
    remaining = pages
    total_chunks = (pages + SCRIPT_CHUNK_PAGES - 1) // SCRIPT_CHUNK_PAGES
    chunk_no = 0

    while remaining > 0:
        step = min(SCRIPT_CHUNK_PAGES, remaining)
        start_page = done_pages + 1
        end_page = done_pages + step
        cells = step * 4
        chunk_no += 1
        if note:
            note(f"第 {chunk_no}/{total_chunks} 幕：正在写第 {start_page}-{end_page} 页"
                 f"（本幕约需 {max(1, cells * 30 * 2 // 60)} 分钟）")
        tail = (base_text + "\n\n" + "\n\n".join(chunks)).strip()
        previous = ""
        if tail:
            previous = ("\n前文（接着往下写，不要重复已有情节，不要重新介绍人物）：\n"
                        + tail[-1000:] + "\n")
        prompt = SCRIPT_USER_TMPL.format(
            title=project.get("title") or "未命名",
            style=_style_name(project),
            start_page=start_page,
            end_page=end_page,
            pages=step,
            cells=cells,
            idea=idea_text,
            cast_block=cast,
            scene_block=scenes,
            previous=previous,
            shot_count=len(SHOT_PRESETS),
            shots=SHOT_NAMES,
        )
        text = text_engine.generate(
            prompt, system=SCRIPT_SYSTEM,
            max_length=min(2048, 160 * cells + 200),
            temperature=temperature, on_progress=on_progress,
        )
        text = _clean_script(text)
        if not text:
            if not chunks and not base_text:
                raise LocalTextError("文本模型没有返回有效剧本，请重试或减少页数。")
            break
        chunks.append(text)
        done_pages += step
        remaining -= step

    combined = "\n\n".join(chunks).strip()
    whole = (base_text + "\n\n" + combined).strip() if base_text else combined
    if not whole:
        raise LocalTextError("文本模型没有返回有效剧本，请重试或减少页数。")
    crepo.update_project(project["id"], synopsis=whole)
    return {
        "text": whole,
        "mode": mode,
        "appended_pages": pages - remaining,
        "total_pages": done_pages,
        "cells": done_pages * 4,
        "chunks": len(chunks),
        "seconds": text_engine.last_seconds,
    }


def continue_script(project: dict, *, pages: int = 2, idea: str = "",
                    temperature: float = 0.85, on_progress: ProgressCb = None,
                    note: Optional[Callable[[str], None]] = None) -> dict:
    """在现有剧本后面续写 N 页（把短故事写长）。"""
    return generate_script(project, pages=pages, idea=idea, mode="continue",
                           temperature=temperature, on_progress=on_progress, note=note)


POLISH_SYSTEM = "你是一位资深漫画编剧，擅长在不改变剧情走向的前提下强化画面感与对白张力。"

POLISH_USER_TMPL = """请润色以下漫画分格剧本。

作品名：{title}
{cast_block}
润色要求：
1. 保留原有的分页、格数、景别标记、对白与音效标记，不要增删格子；
2. 画面描述更具体可画（动作、表情、光线），对白更口语化、更有个性；
3. 行首 [景别] 必须保留，且仍然只能使用这些词：{shots}；
4. 只输出润色后的剧本正文，不要任何说明。

原剧本：
{script}
"""


def polish_script(project: dict, *, script: str = "", temperature: float = 0.6,
                  on_progress: ProgressCb = None) -> dict:
    src = (script or project.get("synopsis") or "").strip()
    if not src:
        raise LocalTextError("还没有剧本可以润色，请先生成或粘贴剧本。")
    prompt = POLISH_USER_TMPL.format(
        title=project.get("title") or "未命名",
        cast_block=_cast_block(project),
        shots=SHOT_NAMES,
        script=src,
    )
    text = text_engine.generate(
        prompt, system=POLISH_SYSTEM,
        max_length=min(2048, len(src) * 2 + 300),
        temperature=temperature, on_progress=on_progress,
    )
    text = _clean_script(text)
    if not text:
        raise LocalTextError("文本模型没有返回有效结果，请重试。")
    crepo.update_project(project["id"], synopsis=text)
    return {"text": text, "seconds": text_engine.last_seconds}


def _clean_script(text: str) -> str:
    """去掉模型偶尔带出的 Markdown 包裹与前后说明。"""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    lines = []
    for line in t.splitlines():
        s = line.rstrip()
        if not s.strip():
            lines.append("")
            continue
        # 去掉 "1. " / "- " / "**" 等编号与强调
        s = re.sub(r"^\s*(?:[-*•]|\d+[.、)])\s*", "", s)
        s = s.replace("**", "")
        lines.append(s.strip())
    out = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", out)


# ============================================================
#  二、角色提取
# ============================================================

CHARACTER_SYSTEM = (
    "你是一位影视角色设计师，擅长从剧本里提炼人物，并给出可直接用于 AI 绘图的角色设定。"
    "你只输出 JSON，不输出任何多余文字。"
)

CHARACTER_USER_TMPL = """请阅读以下漫画剧本，提取其中有台词或推动情节的人物，为每个人物设计完整设定。

作品名：{title}
画风：{style}
剧本：
{script}

输出 JSON 数组（不要用 Markdown 代码块包裹），每个元素格式：
{{
  "name": "人物姓名",
  "role": "主角 或 配角 或 反派 或 龙套",
  "gender": "男 或 女 或其他",
  "age": "年龄或年龄段，如 17 岁 / 中年",
  "personality": "性格，20 字以内",
  "appearance": "外貌，30 字以内：发色发型、瞳色、五官特征、体型",
  "outfit": "服装，25 字以内：上装、下装、鞋、标志性配饰",
  "background": "人物小传，30 字以内",
  "detail_prompt": "英文角色描述，用于 AI 绘图，包含 gender, age, face shape, facial features, hairstyle and color, clothing structure and colors, accessories。不要写构图、背景、镜头、分栏"
}}

要求：提取 {count} 个以内的主要人物，按重要程度排序；只输出 JSON 数组。
"""


def extract_characters(project: dict, *, count: int = 6, script: str = "",
                       replace: bool = False, temperature: Optional[float] = None,
                       on_progress: ProgressCb = None) -> dict:
    """用本地文本模型从剧本里提取角色并落库。"""
    src = (script or project.get("synopsis") or "").strip()
    if not src:
        raise LocalTextError("还没有剧本，请先在「剧本」步骤生成或粘贴剧本。")
    count = max(1, min(int(count or 6), 12))
    prompt = CHARACTER_USER_TMPL.format(
        title=project.get("title") or "未命名",
        style=_style_name(project),
        script=src[:4000],
        count=count,
    )
    raw = text_engine.generate(
        prompt, system=CHARACTER_SYSTEM,
        max_length=min(3072, 320 * count + 200),
        temperature=temperature, on_progress=on_progress,
    )
    items = _parse_json_array(raw)
    if not items:
        raise LocalTextError(
            "没有从模型输出里解析到角色。可重试一次；若持续失败，请把剧本写得再明确一些。"
        )

    existing = {c["name"]: c for c in crepo.list_characters(project["id"])}
    if replace:
        for c in crepo.list_characters(project["id"]):
            crepo.delete_character(c["id"])
        existing = {}

    created, updated = 0, 0
    for item in items[:count]:
        name = _as_text(item, "name")
        if not name:
            continue
        fields = {
            "role": _as_text(item, "role", "配角") or "配角",
            "gender": _as_text(item, "gender"),
            "age": _as_text(item, "age"),
            "personality": _as_text(item, "personality"),
            "appearance": _as_text(item, "appearance"),
            "outfit": _as_text(item, "outfit"),
            "background": _as_text(item, "background"),
            "detail_prompt": _as_text(item, "detail_prompt"),
        }
        hit = existing.get(name)
        if hit:
            crepo.update_character(hit["id"], **fields)
            updated += 1
        else:
            crepo.create_character(project["id"], name=name, **fields)
            created += 1
    return {"created": created, "updated": updated, "total": len(items),
            "seconds": text_engine.last_seconds,
            "characters": crepo.list_characters(project["id"])}


# ============================================================
#  三、场景提取
# ============================================================

SCENE_SYSTEM = (
    "你是一位影视美术指导，擅长把剧本场景转化为清晰的视觉设定与可直接用于 AI 绘图的英文提示词。"
    "你只输出 JSON，不输出任何多余文字。"
)

SCENE_USER_TMPL = """请阅读以下漫画剧本，提取其中出现的主要场景，为每个场景做视觉设定。

作品名：{title}
画风：{style}
{cast_block}
剧本：
{script}

输出 JSON 数组（不要用 Markdown 代码块包裹），每个元素格式：
{{
  "name": "场景名，6 字以内，如 天台雨夜",
  "location": "具体地点，如 市中心写字楼天台",
  "time_of_day": "日 或 夜 或 黄昏 或 清晨",
  "atmosphere": "氛围关键词，20 字以内，如 阴冷压抑、霓虹反光",
  "desc": "画面描述，40 字以内，含主要陈设与光源",
  "prompt": "英文场景概念图提示词：environment only, no characters, 含建筑样式、陈设、材质、光源与氛围；明确时代与地域特征，避免与设定冲突的元素"
}}

要求：提取 {count} 个以内的主要场景，按出场重要性排序；只输出 JSON 数组。
"""


def extract_scenes(project: dict, *, count: int = 6, script: str = "",
                   replace: bool = False, temperature: Optional[float] = None,
                   on_progress: ProgressCb = None) -> dict:
    """用本地文本模型从剧本里提取场景并落库。"""
    src = (script or project.get("synopsis") or "").strip()
    if not src:
        raise LocalTextError("还没有剧本，请先在「剧本」步骤生成或粘贴剧本。")
    count = max(1, min(int(count or 6), 12))
    prompt = SCENE_USER_TMPL.format(
        title=project.get("title") or "未命名",
        style=_style_name(project),
        cast_block=_cast_block(project),
        script=src[:4000],
        count=count,
    )
    raw = text_engine.generate(
        prompt, system=SCENE_SYSTEM,
        max_length=min(3072, 320 * count + 200),
        temperature=temperature, on_progress=on_progress,
    )
    items = _parse_json_array(raw)
    if not items:
        raise LocalTextError("没有从模型输出里解析到场景，请重试或把剧本写得更具体。")

    existing = {s["name"]: s for s in crepo.list_scenes(project["id"])}
    if replace:
        for s in crepo.list_scenes(project["id"]):
            crepo.delete_scene(s["id"])
        existing = {}

    created, updated = 0, 0
    for item in items[:count]:
        name = _as_text(item, "name")
        if not name:
            continue
        fields = {
            "location": _as_text(item, "location"),
            "time_of_day": _as_text(item, "time_of_day"),
            "atmosphere": _as_text(item, "atmosphere"),
            "desc": _as_text(item, "desc"),
            "prompt": _as_text(item, "prompt"),
        }
        hit = existing.get(name)
        if hit:
            crepo.update_scene(hit["id"], **fields)
            updated += 1
        else:
            crepo.create_scene(project["id"], name=name, **fields)
            created += 1
    return {"created": created, "updated": updated, "total": len(items),
            "seconds": text_engine.last_seconds,
            "scenes": crepo.list_scenes(project["id"])}


# ============================================================
#  四、智能分镜（LLM 拆镜）
# ============================================================

STORYBOARD_SYSTEM = (
    "你是一位专业漫画分镜师，擅长把剧本拆成可执行的格子脚本，并严格按 JSON 格式输出。"
)

STORYBOARD_USER_TMPL = """请把下面的漫画剧本拆解成分镜脚本，输出 JSON 数组。

{cast_block}{scene_block}
剧本：
{script}

输出 JSON 数组（不要用 Markdown 代码块包裹），每个元素格式：
{{
  "page": 页码（从 1 开始）,
  "shot": "景别，只能取：{shots}",
  "scene_ref": "该格所属场景名（从上面的场景设定里选，没有匹配就留空字符串）",
  "scene": "动作与场景描述：谁、在哪、在做什么、什么表情，35 字以内，必须写人物完整姓名，禁止出现引号",
  "dialogue": "该格台词或旁白，没有就填空字符串",
  "sfx": "该格拟声词，没有就填空字符串"
}}

要求（最重要的一条：对白绝对不能丢）：
1. 保留原剧本的分页与格数，一格都不能少，顺序不能变；
2. **原剧本里每一个「」或 “” 包裹的台词，都必须原样放进该格的 dialogue 字段**，
   一条都不能漏，也不要合并到相邻格；剧本里没有台词时 dialogue 才填空字符串；
3. scene 字段只写画面，不要把台词写进 scene，也不要出现任何引号；
4. 有对白的格子尽量用中景/全景/远景/背影，避免清晰口部特写；
5. 只输出 JSON 数组，不要任何解释。

输出前自查：dialogue 非空的格子数量，必须等于原剧本里带台词的格子数量。
"""


def _recover_dialogue(items: list[dict], script: str) -> int:
    """把模型漏掉的对白补回来。返回修补的格数。

    本机 8B 模型经常：① 直接把对白并进 scene；② 只挑几条写。
    这里做两道兜底：
      a) 从 scene 文本里的「」把台词汇总出来（并把它从画面描述里摘掉）；
      b) 与规则解析器解析同一份剧本的结果按序对齐，缺什么补什么。
    """
    fixed = 0

    # (a) 先从 scene 里回收引号内容
    for it in items:
        scene_text = _as_text(it, "scene")
        if not scene_text:
            continue
        found = [x.strip() for x in _QUOTE_RE.findall(scene_text) if x.strip()]
        if not found:
            continue
        kept = _QUOTE_RE.sub(" ", scene_text)
        kept = re.sub(r"\s{2,}", " ", kept).strip(" ，,。;；")
        if not _as_text(it, "dialogue"):
            it["dialogue"] = " / ".join(found)
            fixed += 1
        # 无论对白是否已有，都要把引号内容从画面描述里摘掉
        for key in _FIELD_ALIASES["scene"]:
            if key in it:
                it[key] = kept or scene_text
                break

    # (b) 再与规则解析结果按序对齐补齐
    #     模型通常是「少写」而不是乱序，所以按顺序 zip 最稳；
    #     只填空值，绝不覆盖模型自己写了的内容。
    parsed = parse_script(script)
    for it, src in zip(items, parsed):
        if not _as_text(it, "dialogue") and src.get("dialogue"):
            it["dialogue"] = src["dialogue"]
            fixed += 1
        if not _as_text(it, "sfx") and src.get("sfx"):
            it["sfx"] = src["sfx"]
        if not _as_text(it, "shot") and src.get("shot"):
            it["shot"] = src["shot"]
    return fixed


def generate_storyboard(project: dict, *, script: str = "", replace: bool = True,
                        temperature: Optional[float] = None,
                        on_progress: ProgressCb = None) -> dict:
    """LLM 拆镜：结果写入 comic_panels，失败时由调用方回退到规则解析。"""
    src = (script or project.get("synopsis") or "").strip()
    if not src:
        raise LocalTextError("还没有剧本，请先在「剧本」步骤生成或粘贴剧本。")
    layout = LAYOUT_PRESETS.get(project.get("layout") or "", {})
    cells_per_page = max(1, int(layout.get("cols", 2)) * int(layout.get("rows", 2)))

    prompt = STORYBOARD_USER_TMPL.format(
        cast_block=_cast_block(project),
        scene_block=_scene_block(project),
        script=src[:4000],
        shots=SHOT_NAMES,
    )
    raw = text_engine.generate(
        prompt, system=STORYBOARD_SYSTEM,
        max_length=min(4096, len(src) * 3 + 400),
        temperature=temperature, on_progress=on_progress,
    )
    items = _parse_json_array(raw)
    if not items:
        raise LocalTextError("没能从模型输出里解析出分镜，可重试一次。")

    # 对白兜底：模型漏写的台词在这里补回来（用户明确反馈过这条）
    recovered = _recover_dialogue(items, src)

    scenes = {s["name"]: s["id"] for s in crepo.list_scenes(project["id"])}
    chars = {c["name"]: c["id"] for c in crepo.list_characters(project["id"])}
    name_to_key = {v["name"]: k for k, v in SHOT_PRESETS.items()}
    name_to_key.update({"全景": "full", "动作镜头": "action", "过肩镜头": "over", "背影": "back"})

    if replace:
        for p in crepo.list_panels(project["id"]):
            crepo.delete_panel(p["id"])

    created = 0
    for i, item in enumerate(items):
        scene_text = _as_text(item, "scene")
        dialogue = _as_text(item, "dialogue")
        sfx = _as_text(item, "sfx")
        if not scene_text and not dialogue:
            continue
        shot_raw = _as_text(item, "shot")
        shot_key = name_to_key.get(shot_raw) or _guess_shot(shot_raw) or "medium"
        scene_ref = _as_text(item, "scene_ref")
        # 把出现在描述或对白里的角色名绑定到该格
        blob = f"{scene_text} {dialogue}"
        char_ids = [cid for cname, cid in chars.items() if cname and cname in blob]
        page = _pick(item, "page", None)
        try:
            page = max(1, int(page))
        except Exception:
            page = i // cells_per_page + 1
        crepo.create_panel(
            project["id"],
            seq=i + 1, page=page, cell=i % cells_per_page,
            shot=shot_key, scene=scene_text, dialogue=dialogue, sfx=sfx,
            character_ids=char_ids,
            scene_id=scenes.get(scene_ref),
        )
        created += 1

    if not created:
        raise LocalTextError("模型返回的分镜内容为空，请重试。")
    crepo.renumber(project["id"], page_size=cells_per_page)
    panes = crepo.list_panels(project["id"])
    dialogue_cells = sum(1 for p in panes if (p.get("dialogue") or "").strip())
    return {"created": created, "dialogue_cells": dialogue_cells,
            "dialogue_recovered": recovered,
            "seconds": text_engine.last_seconds, "panels": panes}


def _guess_shot(text: str) -> Optional[str]:
    """模型偶尔会写「中景·湿漉」这类复合词，做一次模糊匹配。"""
    if not text:
        return None
    for key, val in SHOT_PRESETS.items():
        if val["name"] in text:
            return key
    return None


# ============================================================
#  JSON 容错解析
# ============================================================

_FENCE_RE = re.compile(r"```[a-zA-Z]*\s*(.*?)```", re.S)


def _parse_json_array(text: str) -> list[dict]:
    """从模型输出里尽量稳地抠出一个 JSON 数组。

    本地小模型可能带出 ```json 包裹、前后寒暄、或数组后面多一段解释，
    这里按「去包裹 → 直接解析 → 截取首尾方括号 → 逐对象抢救」的顺序退让。
    """
    if not text:
        return []
    t = text.strip()

    m = _FENCE_RE.search(t)
    if m:
        t = m.group(1).strip()

    data = _try_load(t)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("items", "characters", "scenes", "shots", "panels", "data", "list"):
            if isinstance(data.get(key), list):
                return [x for x in data[key] if isinstance(x, dict)]

    start, end = t.find("["), t.rfind("]")
    if start != -1 and end > start:
        data = _try_load(t[start:end + 1])
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        return _salvage_objects(t[start:end + 1])
    return _salvage_objects(t)


def _try_load(text: str) -> Any:
    for candidate in (text, _repair(text)):
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


def _repair(text: str) -> str:
    """修掉尾逗号、中文引号、单引号 JSON 这类常见毛病。"""
    t = text.strip()
    t = t.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    t = re.sub(r",\s*([\]\}])", r"\1", t)
    return t


def _salvage_objects(text: str) -> list[dict]:
    """最后兜底：用括号配对逐个抠出 {...} 对象。"""
    out: list[dict] = []
    depth, start = 0, -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                obj = _try_load(text[start:i + 1])
                if isinstance(obj, dict):
                    out.append(obj)
                start = -1
    return out


# ============================================================
#  服务状态（给前端做可用性提示）
# ============================================================

# ============================================================
#  本地 AI 任务管理
# ============================================================
#
# 本机文本推理在 CPU 上很慢（200 字输出可能 5-10 分钟），如果直接放在
# HTTP 请求里会超时、也会堵住事件循环。这里统一做成单并发后台任务，
# 前端提交后轮询进度，体验和出图队列一致。

import queue  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from inspect import signature as _signature  # noqa: E402

KIND_LABELS = {
    "script_generate": ("AI 写剧本", "generate_script"),
    "script_continue": ("AI 续写剧本", "continue_script"),
    "script_polish": ("AI 润色剧本", "polish_script"),
    "characters_extract": ("AI 提取角色", "extract_characters"),
    "scenes_extract": ("AI 提取场景", "extract_scenes"),
    "storyboard": ("AI 智能分镜", "generate_storyboard"),
}


class AiTaskManager:
    """单并发执行本地文本任务，保留最近任务状态供前端轮询。"""

    MAX_KEEP = 40

    def __init__(self) -> None:
        self._tasks: dict[str, dict] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._boot()

    def _boot(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._loop, name="comic-ai", daemon=True)
        self._worker.start()

    # ---------- 对外 ----------
    def submit(self, kind: str, project_id: str, **params: Any) -> dict:
        if kind not in KIND_LABELS:
            raise LocalTextError(f"未知的 AI 任务类型：{kind}")
        self._boot()
        task_id = "ait_" + uuid.uuid4().hex[:12]
        label, _func = KIND_LABELS[kind]
        task = {
            "id": task_id,
            "kind": kind,
            "label": label,
            "project_id": project_id,
            "params": params,
            "status": "queued",
            "elapsed": 0.0,
            "message": "已排队",
            "result": None,
            "error": None,
            "created_at": time.time(),
        }
        with self._lock:
            self._tasks[task_id] = task
            self._order.append(task_id)
            while len(self._order) > self.MAX_KEEP:
                self._tasks.pop(self._order.pop(0), None)
        self._queue.put(task_id)
        return dict(task)

    def get(self, task_id: str) -> Optional[dict]:
        with self._lock:
            task = self._tasks.get(task_id)
            return dict(task) if task else None

    def list_recent(self, project_id: Optional[str] = None, limit: int = 10) -> list[dict]:
        with self._lock:
            items = [self._tasks[i] for i in reversed(self._order) if i in self._tasks]
        if project_id:
            items = [t for t in items if t["project_id"] == project_id]
        return [dict(t) for t in items[:limit]]

    def running(self) -> Optional[dict]:
        with self._lock:
            for i in reversed(self._order):
                t = self._tasks.get(i)
                if t and t["status"] in ("queued", "running"):
                    return dict(t)
        return None

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task["status"] not in ("queued", "running"):
                return False
            task["status"] = "canceled"
            task["message"] = "已取消"
        text_engine.cancel()
        return True

    # ---------- 执行 ----------
    def _loop(self) -> None:
        while True:
            task_id = self._queue.get()
            if task_id is None:
                return
            with self._lock:
                task = self._tasks.get(task_id)
            if not task or task["status"] == "canceled":
                continue
            self._run(task)

    def _run(self, task: dict) -> None:
        project = crepo.get_project(task["project_id"])
        if not project:
            self._finish(task, error="项目不存在")
            return

        with self._lock:
            task["status"] = "running"
            task["message"] = "本地模型推理中（CPU 推理较慢，请耐心等待）"
            task["started_at"] = time.time()

        def on_progress(seconds: float) -> None:
            with self._lock:
                task["elapsed"] = seconds
                task["message"] = f"本地模型推理中，已用 {int(seconds)} 秒"

        params = dict(task["params"])
        try:
            _label, func_name = KIND_LABELS[task["kind"]]
            func = globals()[func_name]
            # 分幕生成这类长任务需要主动汇报子步骤，函数声明了 note 就注入
            sig = _signature(func)
            if "note" in sig.parameters:
                params["note"] = lambda text, _t=task: self._note(_t, text)
            result = func(project, on_progress=on_progress, **params)
            self._finish(task, result=result)
        except LocalTextError as exc:
            self._finish(task, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - 兜底，错误要透出给界面
            import traceback

            print(f"[漫画AI任务失败] {task['kind']}\n{traceback.format_exc()}", flush=True)
            self._finish(task, error=f"{exc.__class__.__name__}: {exc}")

    def _note(self, task: dict, text: str) -> None:
        """更新任务提示（不覆盖进度时间）。"""
        with self._lock:
            if task["status"] in ("queued", "running"):
                task["message"] = text

    def _finish(self, task: dict, result: Any = None, error: str = "") -> None:
        with self._lock:
            if task["status"] == "canceled":
                return
            task["status"] = "failed" if error else "done"
            task["error"] = error or None
            task["result"] = result
            task["elapsed"] = round(task.get("elapsed") or 0, 1)
            task["message"] = error or f"完成，用时 {task['elapsed']} 秒"


ai_tasks = AiTaskManager()


def llm_status() -> dict:
    st = text_engine.status()
    st["shots"] = [v["name"] for v in SHOT_PRESETS.values()]
    st["styles"] = [v["name"] for v in STYLE_PRESETS.values()]
    st["speed_note"] = (
        "本机为 CPU 文本推理：短输出约 20-60 秒，200 字以上可能 5-10 分钟，"
        "请耐心等待，期间不要重复提交。"
    )
    return st
