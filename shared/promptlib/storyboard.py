# -*- coding: utf-8 -*-
"""叙事模板、JSON 解析、校验修复、无模型降级。

这个模块是「导演层」的全部智能，但它**不做创作**——它只做三件事：
    1. 给模型一份足够窄的合同（只要五项字段）
    2. 把模型吐出来的东西修成合法结构（8B 模型一定会出格式问题）
    3. 模型完全不可用时，用规则凑一份能用的分镜，让链路不断

第 3 点不是兜底代码，是可用性设计：`LLM_ENABLED=false` 或显存被占满时，
用户仍然应该能把「一句话」走到「一条成片」，只是文案平庸一些。
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from shared.promptlib import shots
from shared.promptlib.styles import style_of


# ============================================================
#  叙事模板
# ============================================================

BEAT_PRESETS: dict[str, dict] = {
    "3act": {
        "label": "三幕·萌点 / 反转 / 暖心",
        "desc": "最短最通用，适合 20-40 秒的竖屏短片",
        "slots": [
            {"beat": "萌点", "weight": 0.8,
             "hint": "用一个具体的小动作或小表情立住角色，别急着讲事"},
            {"beat": "反转", "weight": 1.0,
             "hint": "给出一个意外：期待落空、身份揭穿，或一件小事被放大"},
            {"beat": "暖心", "weight": 1.3,
             "hint": "把反转收回到一个温柔的落点上，可以不说破"},
        ],
    },
    "4beat": {
        "label": "四段·日常 Vlog",
        "desc": "像一段没有旁白也看得懂的小日常",
        "slots": [
            {"beat": "开场", "weight": 0.9, "hint": "交代时间地点，给一个环境全景"},
            {"beat": "起事", "weight": 1.0, "hint": "角色开始做一件小事，出现阻碍"},
            {"beat": "使劲", "weight": 1.1, "hint": "努力的过程，动作幅度最大的一格"},
            {"beat": "收尾", "weight": 1.2, "hint": "结果揭晓，情绪落地"},
        ],
    },
    "5beat": {
        "label": "五段·知识讲解",
        "desc": "钩子→反常识→内幕→真相→收束，适合科普口播改编",
        "slots": [
            {"beat": "钩子", "weight": 0.7, "hint": "一句反直觉的断言，抓住注意力"},
            {"beat": "反常识", "weight": 1.0, "hint": "指出大家通常的误解"},
            {"beat": "内幕", "weight": 1.0, "hint": "给出一个别人不知道的细节"},
            {"beat": "真相", "weight": 1.2, "hint": "把逻辑讲通，给出结论"},
            {"beat": "收束", "weight": 1.2, "hint": "回扣开头，留一句有余味的话"},
        ],
    },
}

DEFAULT_PRESET = "3act"
MIN_SHOT_SECONDS = 2.0
MAX_BEATS = 12


def preset_of(key: Optional[str]) -> dict:
    return BEAT_PRESETS.get(str(key or ""), BEAT_PRESETS[DEFAULT_PRESET])


def preset_key(key: Optional[str]) -> str:
    k = str(key or "")
    return k if k in BEAT_PRESETS else DEFAULT_PRESET


def slots_for(preset: str, count: int) -> list[dict]:
    """按预设铺 count 个节拍槽；超长时循环并加序号，短时截取。"""
    base = preset_of(preset)["slots"]
    count = max(1, min(int(count or len(base)), MAX_BEATS))
    out: list[dict] = []
    for i in range(count):
        slot = dict(base[i % len(base)])
        if i >= len(base):
            slot["beat"] = f"{slot['beat']}·{i // len(base) + 1}"
        out.append(slot)
    return out


# ============================================================
#  模型合同
# ============================================================

SYSTEM_PROMPT = """你是一位竖屏短视频分镜师。你只输出一个 JSON 对象，不写任何解释、前言、后缀，不使用 Markdown 代码块。

JSON 结构（字段名必须完全照写，用英文键）：
{"title":"片名，不超过 12 个字","beats":[{"narration":"旁白，一句中文口语，15 到 28 个字","action":"画面里发生什么，只写看得见的动作，不超过 30 个字","shot":"特写 或 近景 或 中景 或 全景 或 远景","camera":"push_in 或 pull_out 或 pan_left 或 pan_right 或 tilt_up 或 tilt_down 或 still"}]}

硬性要求：
1. beats 的数量必须恰好是 {n} 个，不多不少。
2. 每一格只写一个动作，不要在一格里塞两件事。
3. narration 是给人听的，要口语，不要书面语，不要用"首先其次最后"。
4. action 必须能被画出来：写"抬起手挡雨"，不要写"心里有点难过"。
5. 相邻两格的 shot 尽量不要相同，全片至少出现两种景别。
6. 不要出现品牌名、真人姓名、真实地名。"""


def build_prompt(*, topic: str, style_id: str = "", preset: str = "3act",
                 count: int = 3, total_seconds: float = 24.0,
                 tone: str = "", extra: str = "") -> tuple[str, str]:
    """返回 (system, user)。"""
    s = style_of(style_id)
    slots = slots_for(preset, count)
    beat_lines = []
    for i, slot in enumerate(slots, 1):
        beat_lines.append(f"  第 {i} 格（{slot['beat']}）：{slot['hint']}")
    user = (
        f"选题：{topic.strip()}\n"
        f"画风：{s['label']}，{s['ratio']}，{s['render']}\n"
        f"成片时长：约 {total_seconds:.0f} 秒，共 {len(slots)} 格\n"
        + (f"调性：{tone.strip()}\n" if tone.strip() else "")
        + (f"补充要求：{extra.strip()}\n" if extra.strip() else "")
        + "各格节拍要求：\n" + "\n".join(beat_lines) + "\n"
        + "现在输出 JSON。"
    )
    return SYSTEM_PROMPT.replace("{n}", str(len(slots))), user


# ============================================================
#  JSON 提取与修复
# ============================================================

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def _balanced_objects(text: str):
    """扫描出所有顶层平衡的 {...} 片段（跳过字符串内的括号与转义）。"""
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield text[start:i + 1]
                    start = -1


def extract_json(text: str) -> Optional[Any]:
    """从模型原始输出里挖出 JSON。

    8B 模型的实际输出长这样（很常见）：
        "好的，我来为你设计分镜：\\n```json\\n{...}\\n```\\n希望你喜欢！"
    所以不能直接 json.loads，要按「代码块 → 平衡括号 → 整体」三级尝试。
    """
    raw = (text or "").strip()
    if not raw:
        return None

    # 1) 整体就是 JSON
    try:
        return json.loads(raw)
    except Exception:
        pass

    # 2) 代码块
    for m in _FENCE.finditer(raw):
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            continue

    # 3) 平衡括号，取最长的一个（最可能是完整对象）
    candidates = []
    for frag in _balanced_objects(raw):
        try:
            candidates.append((len(frag), json.loads(frag)))
        except Exception:
            continue
    if candidates:
        candidates.sort(key=lambda x: -x[0])
        return candidates[0][1]

    # 4) 顶层是数组
    m = re.search(r"\[.*\]", raw, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


_NARR_KEYS = ("narration", "旁白", "voiceover", "text", "line", "字幕")
_ACTION_KEYS = ("action", "动作", "画面", "visual", "description")
_SHOT_KEYS = ("shot", "景别", "shot_size", "size")
_CAM_KEYS = ("camera", "镜头", "camera_move", "move")
_TITLE_KEYS = ("title", "片名", "name")
_BEATS_KEYS = ("beats", "分镜", "shots", "scenes", "segments")


def _pick(d: dict, keys) -> str:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return str(d[k]).strip()
    return ""


def _clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    s = s.strip("“”\"'　 ")
    # 片名/旁白不需要句尾标点，截断处留半个句号尤其难看
    return s[:n].rstrip("。，！？、,.！？…：:；; ")


def _norm_shot(v: str) -> str:
    v = (v or "").strip()
    for z in shots.SHOT_SIZES:
        if z in v:
            return z
    # 模型爱写英文
    en = {"close": "特写", "closeup": "特写", "medium": "中景",
          "wide": "全景", "full": "全景", "extreme": "远景", "long": "远景"}
    low = v.lower().replace("-", "").replace("_", "")
    for k, z in en.items():
        if k in low:
            return z
    return "中景"


def _norm_cam(v: str) -> str:
    v = (v or "").strip().lower().replace("-", "_").replace(" ", "_")
    if v in shots.CAMERA_MOVES:
        return v
    alias = {
        "zoom_in": "push_in", "zoomin": "push_in", "推": "push_in", "推近": "push_in",
        "zoom_out": "pull_out", "zoomout": "pull_out", "拉": "pull_out", "拉远": "pull_out",
        "pan_left_": "pan_left", "左移": "pan_left", "left": "pan_left",
        "右移": "pan_right", "right": "pan_right",
        "上移": "tilt_up", "up": "tilt_up",
        "下移": "tilt_down", "down": "tilt_down",
        "static": "still", "fixed": "still", "静止": "still", "定格": "still",
    }
    if v in alias:
        return alias[v]
    for k, c in alias.items():
        if k in v:
            return c
    return shots.DEFAULT_MOVE


def normalize(raw: Any, *, preset: str, count: int, topic: str = "") -> dict:
    """把任意模型输出修成合法分镜。

    规则：字段缺失就补、值非法就归一到合法值、段数不对就补/截。
    永远返回可用结果，不抛错——分镜这一步失败了，后面整条链路就断了。

    空格必须补
    ----------
    8B 模型经常只写够前两格就收尾。空的格子在界面里是个洞，用户没法出图，
    整条链路就断在那里。所以空白格用规则文案（fallback）按位补上——宁可
    文案平庸，也不能让某一格无法继续。
    """
    slots = slots_for(preset, count)
    spare = fallback(topic, preset=preset, count=count)["beats"] if topic else []
    title = ""
    items: list[Any] = []

    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        title = _pick(raw, _TITLE_KEYS)
        for k in _BEATS_KEYS:
            if isinstance(raw.get(k), list):
                items = raw[k]
                break
        # 模型有时把 beats 包一层 {"故事分镜": [...]}
        if not items:
            for v in raw.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    items = v
                    break
    elif isinstance(raw, str) and raw.strip():
        items = [{"narration": raw}]

    beats: list[dict] = []
    filled = 0
    for i, slot in enumerate(slots):
        src = items[i] if i < len(items) and isinstance(items[i], dict) else {}
        action = _clip(_pick(src, _ACTION_KEYS), 40)
        narration = _clip(_pick(src, _NARR_KEYS), 40)
        shot = _pick(src, _SHOT_KEYS)
        cam = _pick(src, _CAM_KEYS)
        if action or narration:
            filled += 1
            # 只给了一半的情况很常见：旁白当动作、动作当旁白，都算有效信息
            if not action:
                action = narration
            if not narration:
                narration = action
        else:
            # 这一格模型完全没写 → 用规则文案补位，但不覆盖它给的镜头选择
            fb = spare[i] if i < len(spare) else {}
            action = fb.get("action") or "安静地站着，看向镜头"
            narration = fb.get("narration") or ""
            shot = shot or fb.get("shot", "")
            cam = cam or fb.get("camera", "")
        beats.append({
            "index": i + 1,
            "beat": slot["beat"],
            "hint": slot["hint"],
            "weight": float(slot["weight"]),
            "narration": narration,
            "action": action,
            "shot": _norm_shot(shot),
            "camera": _norm_cam(cam),
        })
    return {"title": _clip(title, 16), "beats": beats, "filled": filled}


# ============================================================
#  规则降级（无 LLM）
# ============================================================

_SENT_SPLIT = re.compile(r"[。！？!?；;\n]+")
_COMMA_SPLIT = re.compile(r"[，,、]")


def _split_topic(topic: str, count: int) -> list[str]:
    """把一句话/一小段文字切成 count 个语义片段。"""
    text = (topic or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in _SENT_SPLIT.split(text) if p.strip()]
    if len(parts) < count:
        # 按逗号再切一层
        extra: list[str] = []
        for p in parts:
            extra.extend([x.strip() for x in _COMMA_SPLIT.split(p) if x.strip()])
        parts = extra or parts
    if len(parts) < count and parts:
        # 还是不够：按字数均分
        joined = "".join(parts)
        step = max(1, len(joined) // count)
        parts = [joined[i:i + step] for i in range(0, len(joined), step)]
    return parts[:count]


_FALLBACK_ACTIONS = [
    "角色站在画面中央，抬头看向远处",
    "角色低头看了看自己的手，又抬起头",
    "角色向前走了两步，停下来",
    "角色侧过身，回头看向镜头",
    "角色缓缓转过身，背对镜头",
    "角色坐在原地，双手搭在膝盖上",
    "角色举起一只手，像是在打招呼",
    "角色慢慢蹲下来，缩成一团",
]

_FALLBACK_SHOTS = ["全景", "近景", "中景", "特写", "中景", "近景", "远景", "中景"]
_FALLBACK_CAMS = ["push_in", "still", "pan_right", "push_in", "tilt_up", "pull_out", "pan_left", "still"]


def fallback(topic: str, *, preset: str, count: int) -> dict:
    """没有可用的语言模型时，用规则凑一份结构完整的写分镜。

    旁白直接取用户原文的片段（所以用户写得越具体，成片越像样），
    动作与镜头按固定节奏轮换，保证景别有变化、镜头不重复。
    """
    slots = slots_for(preset, count)
    frags = _split_topic(topic, len(slots))
    # 片名取第一个完整短句，比「前 12 个字切到一半」体面得多
    first_sentence = _SENT_SPLIT.split(topic or "")[0] if topic else ""
    beats = []
    for i, slot in enumerate(slots):
        frag = frags[i] if i < len(frags) else ""
        beats.append({
            "index": i + 1,
            "beat": slot["beat"],
            "hint": slot["hint"],
            "weight": float(slot["weight"]),
            "narration": _clip(frag, 40) or f"{slot['beat']}：{_clip(topic, 20)}",
            "action": _FALLBACK_ACTIONS[i % len(_FALLBACK_ACTIONS)],
            "shot": _FALLBACK_SHOTS[i % len(_FALLBACK_SHOTS)],
            "camera": _FALLBACK_CAMS[i % len(_FALLBACK_CAMS)],
        })
    return {"title": _clip(first_sentence, 12) or "无题",
            "beats": beats, "filled": len(beats)}


# ============================================================
#  时长分配
# ============================================================

def plan_durations(beats: list[dict], total_seconds: float,
                   xfade: float = 0.5) -> list[float]:
    """按节拍权重分配每段时长。

    注意转场会吃掉时间：n 段用 xfade 各重叠 xfade 秒，成片总长会缩短
    (n-1)*xfade。所以分配时先把这个损耗加回去，用户设的「30 秒」才是真的 30 秒。
    """
    n = len(beats)
    if n == 0:
        return []
    target_sum = max(float(total_seconds) + (n - 1) * xfade, n * MIN_SHOT_SECONDS)
    weights = [max(0.1, float(b.get("weight") or 1.0)) for b in beats]
    wsum = sum(weights)
    raw = [target_sum * w / wsum for w in weights]

    # 保底：不足最小时长的先抬到最小，多出来的从最长的段里扣
    for _ in range(4):
        short = [i for i, d in enumerate(raw) if d < MIN_SHOT_SECONDS]
        if not short:
            break
        deficit = sum(MIN_SHOT_SECONDS - raw[i] for i in short)
        for i in short:
            raw[i] = MIN_SHOT_SECONDS
        donors = sorted((i for i in range(n) if raw[i] > MIN_SHOT_SECONDS + 0.3),
                        key=lambda i: -raw[i])
        for i in donors:
            if deficit <= 0:
                break
            give = min(deficit, raw[i] - MIN_SHOT_SECONDS)
            raw[i] -= give
            deficit -= give
        if deficit > 0:
            break

    return [round(d, 2) for d in raw]
