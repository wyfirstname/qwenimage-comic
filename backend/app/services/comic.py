# -*- coding: utf-8 -*-
"""漫画工作室业务服务。

职责：
  1. 风格 / 景别 / 排版预设
  2. 剧本解析为分镜（规则化拆解，无需外部 LLM）
  3. 分镜提示词拼装（画风统一 + 角色一致性策略）
  4. 分镜出图（复用主出图队列，串行执行）
  5. 排版渲染：把分镜图拼成漫画页，绘制对话气泡与拟声词
"""

from __future__ import annotations

import base64
import io
import json
import random
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFont

from app import comic_repo as crepo
from app import rhythm
from app.comic_defaults import (
    ANATOMY_POSITIVE,
    CHARACTER_NEGATIVE_EXTRA,
    DEFAULT_CHAR_NEGATIVE,
    DEFAULT_PROJECT_NEGATIVE,
    PANEL_NEGATIVE_EXTRA,
    SCENE_NEGATIVE_EXTRA,
)
from app.config import settings
from app.gen_profiles import DEFAULT_PROFILE, get_profile, list_profiles
from app.models_util import new_id, now_iso, relative_url, resolve_stored, stored_path
from app.style_library import attribution as style_attribution
from app.style_library import expand_style, library as style_library

# ============================================================
#  预设
# ============================================================

# 本项目原有 5 个画风 key（jp_bw / jp_color / american / ink / webtoon）保留为
# STYLE_PRESETS，供 comic_ai 的旧逻辑与存量项目做兼容查询；
# 实际出图的画风解析一律走 style_library.expand_style()（内含这 5 个的别名条目）。
STYLE_PRESETS: dict[str, dict[str, Any]] = {
    "jp_bw": {
        "name": "日式黑白漫画",
        "desc": "墨线 + 网点，杂志连载质感",
        "en": ("japanese manga style, monochrome black and white, clean ink line art, "
               "screentone shading, strong contrast, detailed linework"),
        "grayscale": True,
    },
    "jp_color": {
        "name": "日式彩色动漫",
        "desc": "赛璐璐上色，明亮通透",
        "en": ("japanese anime style, cel shading, vibrant colors, clean line art, "
               "detailed background, soft lighting"),
        "grayscale": False,
    },
    "american": {
        "name": "美式漫画",
        "desc": "粗描边 + 影调＋半调网点",
        "en": ("american comic book style, bold ink outlines, dramatic chiaroscuro lighting, "
               "halftone dots, dynamic composition"),
        "grayscale": False,
    },
    "ink": {
        "name": "国风水墨",
        "desc": "水墨写意，留白构图",
        "en": ("chinese ink wash painting style, sumi-e brush strokes, elegant negative space, "
               "muted traditional palette, xuan paper texture"),
        "grayscale": False,
    },
    "webtoon": {
        "name": "韩式条漫",
        "desc": "柔和上色，竖屏阅读",
        "en": ("korean webtoon style, soft cel shading, pastel palette, clean digital line art, "
               "polished background"),
        "grayscale": False,
    },
}

SHOT_PRESETS: dict[str, dict[str, str]] = {
    "wide":    {"name": "远景", "en": "extreme wide shot, establishing view, characters small within the environment"},
    "full":    {"name": "全景", "en": "full body shot, characters visible from head to toe"},
    "medium":  {"name": "中景", "en": "medium shot, characters framed from the waist up"},
    "close":   {"name": "近景", "en": "close-up shot, characters framed from the chest up, clear facial expression"},
    "extreme": {"name": "特写", "en": "extreme close-up, intense detail on face and eyes"},
    "action":  {"name": "动作", "en": "dynamic action shot, speed lines, dramatic camera angle"},
    "over":    {"name": "过肩", "en": "over-the-shoulder shot, two characters in conversation framing"},
    "back":    {"name": "背影", "en": "seen from behind, silhouette against the background"},
}

LAYOUT_PRESETS: dict[str, dict[str, Any]] = {
    "grid_1x2":  {"name": "上下两格", "cols": 1, "rows": 2},
    "strip_1x3": {"name": "条漫三格", "cols": 1, "rows": 3},
    "grid_2x2":  {"name": "2×2 四格", "cols": 2, "rows": 2},
    "grid_2x3":  {"name": "2×3 六格", "cols": 2, "rows": 3},
    "grid_3x3":  {"name": "3×3 九格", "cols": 3, "rows": 3},
}

# 分镜自动景别节奏（未指定时循环使用，让页面有呼吸感）
_AUTO_SHOT_CYCLE = ["wide", "medium", "close", "medium", "full", "extreme", "medium", "over"]

_SHOT_NAME_TO_KEY = {v["name"]: k for k, v in SHOT_PRESETS.items()}
_SHOT_NAME_TO_KEY.update({"全景": "full", "动作镜头": "action", "过肩镜头": "over", "背影": "back"})


# ------------------------------------------------------------
#  画风 / 主题色 / 分镜节奏：统一的解析入口
# ------------------------------------------------------------

def resolve_style(project: dict) -> dict:
    """把项目的画风解析成可注入提示词的完整口径（兼容旧 key / FA-xxx / 001）。"""
    return expand_style(project.get("style") or "jp_bw")


def style_prompt_text(project: dict) -> str:
    """该项目的画风提示词文本（keep_style 关闭时返回空串）。"""
    if not project.get("keep_style", 1):
        return ""
    return resolve_style(project).get("prompt_text") or ""


def theme_prompt_text(project: dict) -> str:
    """主题色提示词（一行文本，如 `主题色：克莱因蓝（Klein Blue）。`）。

    主题色还能"外溢"到排版阶段：气泡底色 / 页码 / 页边框都会跟着走（见 _theme_rgb）。
    """
    parts: list[str] = []
    for key in ("theme_color", "theme_color2"):
        line = style_library().color_prompt(project.get(key))
        if line and line not in parts:
            parts.append(line)
    return " ".join(parts)


def project_style_grayscale(project: dict) -> bool:
    return bool(resolve_style(project).get("grayscale"))


# 主题色 hex → RGB；缺省回落到中性灰（用于排版配色）
_DEFAULT_THEME_RGB = (18, 18, 18)
_DEFAULT_THEME2_RGB = (120, 126, 138)


def _hex_to_rgb(hex_str: str) -> Optional[tuple[int, int, int]]:
    s = (hex_str or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return None
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return None


def _theme_rgb(project: dict) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """返回 (主色, 点缀色) 的 RGB；未设置时回落到默认黑 + 灰。"""
    lib = style_library()
    main = _hex_to_rgb(lib.color_hex(project.get("theme_color")))
    accent = _hex_to_rgb(lib.color_hex(project.get("theme_color2")))
    return (main or _DEFAULT_THEME_RGB, accent or main or _DEFAULT_THEME2_RGB)

# 反向提示词常量统一放在 app/comic_defaults.py（数据层迁移也要用同一份文本）

# ============================================================
#  剧本解析
# ============================================================

_PAGE_RE = re.compile(r"^\s*(?:={2,}\s*)?(?:第\s*([0-9一二三四五六七八九十]+)\s*页|PAGE\s*([0-9]+))(?:[:：]|\s*={2,})?\s*$",
                      re.IGNORECASE)
_SHOT_PREFIX_RE = re.compile(r"^\s*[\[【(（]\s*([^\]】)）]+?)\s*[\]】)）]\s*")
_SHOT_LABEL_RE = re.compile(r"^\s*(景别|镜头)\s*[:：]\s*(\S+)\s*")
_DIALOGUE_PATTERNS = [
    re.compile(r"[「『]([^」』]+)[」』]"),
    re.compile(r"[“\"]([^”\"]+)[”\"]"),
]
_SFX_RE = re.compile(r"[\[【(（]\s*(?:音效|SFX|sfx)\s*[:：]?\s*([^\]】)）]+)[\]】)）]")
_SPEAKER_RE = re.compile(r"^\s*([\u4e00-\u9fa5A-Za-z0-9_·]{1,12})\s*[:：]\s*(.*)$")

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _to_int(text: str) -> int:
    text = (text or "").strip()
    if text.isdigit():
        return int(text)
    if text in _CN_NUM:
        return _CN_NUM[text]
    if text.startswith("十") and len(text) == 1:
        return 10
    if "十" in text:
        a, _, b = text.partition("十")
        tens = _CN_NUM.get(a, 1) if a else 1
        ones = _CN_NUM.get(b, 0) if b else 0
        return tens * 10 + ones
    return 1


def parse_script(text: str) -> list[dict[str, Any]]:
    """把剧本拆成分镜列表。

    支持写法：
      * 一行一格；空行分隔
      * 「第 1 页」或 === 第2页 === 分页
      * 行首 [特写] / 景别：近景 指定景别
      * 对话用 「」 “” 包裹，或 角色名：台词
      * [音效：轰隆] 指定拟声词
    """
    panels: list[dict[str, Any]] = []
    page = 1
    auto_index = 0

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m = _PAGE_RE.match(line)
        if m:
            page = _to_int(m.group(1) or m.group(2) or "1")
            continue
        if set(line) <= {"-", "=", "*", "_"} and len(line) >= 3:
            page += 1
            continue

        shot_key: Optional[str] = None

        m = _SHOT_PREFIX_RE.match(line)
        if m:
            token = m.group(1).strip()
            shot_key = _SHOT_NAME_TO_KEY.get(token) or _SHOT_NAME_TO_KEY.get(token.replace("镜头", ""))
            if shot_key:
                line = line[m.end():].strip()

        m = _SHOT_LABEL_RE.match(line)
        if m:
            shot_key = _SHOT_NAME_TO_KEY.get(m.group(2)) or shot_key
            line = line[m.end():].strip()

        dialogue = ""
        for pat in _DIALOGUE_PATTERNS:
            found = pat.findall(line)
            if found:
                dialogue = " / ".join(x.strip() for x in found if x.strip())
                line = pat.sub("", line)
                break

        sfx = ""
        m = _SFX_RE.search(line)
        if m:
            sfx = m.group(1).strip()
            line = _SFX_RE.sub("", line)

        if not dialogue:
            m = _SPEAKER_RE.match(line)
            if m and not line.startswith(("场景", "画面", "提示")):
                speaker, rest = m.group(1), m.group(2).strip()
                # 形如「小明：你好」→ 说话人 + 台词。
                # 注意 line 必须清空：若把 line 置成 speaker，scene 就只剩人名（≤10 字），
                # 会被下方"短行合并"规则当成纯对白行不断并进上一格，
                # 台词密集的剧本会把整段对白全堆进第一格（其余分镜全无对白）。
                # 过长的"X：Y"更像「场景名：画面描述」而非台词——
                # 尤其带景别前缀时（[远景] 鹊桥：牛郎织女立于两端），阈值收得更紧。
                if rest and len(speaker) <= 8 and len(rest) <= (12 if shot_key else 30):
                    dialogue = f"{speaker}：{rest}"
                    line = ""

        scene = re.sub(r"\s{2,}", " ", line).strip(" ,，。;；")
        explicit_shot = shot_key is not None

        # 纯对白 / 纯音效行（如单独一行的「台词」、「旁人议论」、[音效：轰隆]）：
        # 并到上一格，不单独占一格。若该行已显式标注景别，则视为新的一格。
        # 合并必须有上限：上一格已攒了两句对白（或对白已超 80 字）就另起新格，
        # 否则连续的台词行会把整段对白全堆进同一格，其余分镜全部无对白。
        last = panels[-1] if panels else None
        last_segs = (last["dialogue"].count(" / ") + 1) if last and last.get("dialogue") else 0
        if (last is not None and not explicit_shot and (dialogue or sfx)
                and len(scene) <= 10 and last_segs < 2
                and len(last.get("dialogue") or "") <= 80):
            if dialogue:
                last["dialogue"] = f"{last['dialogue']} / {dialogue}" if last.get("dialogue") else dialogue
            if sfx:
                last["sfx"] = f"{last['sfx']} {sfx}".strip() if last.get("sfx") else sfx
            if scene:
                last["scene"] = f"{last['scene']} {scene}".strip() if last.get("scene") else scene
            continue

        if not scene and not dialogue and not sfx:
            continue

        if not shot_key:
            shot_key = _AUTO_SHOT_CYCLE[auto_index % len(_AUTO_SHOT_CYCLE)]
        auto_index += 1

        panels.append({
            "page": max(1, page),
            "shot": shot_key,
            "shot_explicit": explicit_shot,
            "scene": scene,
            "dialogue": dialogue,
            "sfx": sfx,
        })
    return panels


# ============================================================
#  项目输出目录（出图按项目归档，不与其他项目混在同一日期目录）
# ============================================================

# 目录名里不允许出现的字符（Windows 限制 + 换行）
_UNSAFE_DIR_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _safe_dir_name(text: str, max_len: int = 24) -> str:
    """把项目标题清洗成安全的目录名（保留中英文与数字，剔除路径非法字符）。"""
    s = _UNSAFE_DIR_RE.sub("_", (text or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" ._-")
    if not s:
        return "project"
    return s[:max_len].strip(" ._-") or "project"


def project_output_subdir(project: dict, persist: bool = True) -> str:
    """项目专属输出子目录（相对 outputs/），形如 `comics/雨夜天台_1a2b3c`。

    目录名在**首次用到时算出来并写回 comic_projects.output_dir**：
    之后改项目标题不会让已生成的图片搬家（也避免同一项目出现两个目录），
    末尾带 id 尾号保证不同项目重名时也不会撞在一起。
    """
    existing = (project.get("output_dir") or "").strip()
    if existing:
        return existing
    pid = project.get("id") or ""
    tail = re.sub(r"[^0-9a-zA-Z]", "", pid)[-6:] or "000000"
    sub = f"comics/{_safe_dir_name(project.get('title') or '')}_{tail}"
    if persist and pid:
        try:
            crepo.update_project(pid, output_dir=sub)
            project["output_dir"] = sub
        except Exception as exc:  # pragma: no cover - 防御性
            print(f"[comic] 记录项目输出目录失败（不影响出图）：{exc}")
    return sub


def project_output_dir(project: dict) -> Path:
    """项目输出目录的绝对路径（不存在时创建）。"""
    d = settings.output_dir_path / project_output_subdir(project)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _resolve_stored_path(value: Any) -> Optional[Path]:
    """把数据库里存的路径 / 静态 URL 还原成本地路径。

    历史数据里两种都有：`str(Path)` 的绝对路径，以及 `/data/outputs/...` 形式的静态 URL。
    """
    if not value:
        return None
    s = str(value).strip().replace("\\", "/")
    if not s:
        return None
    if s.startswith("/"):
        return settings.project_root / s.lstrip("/")
    if s.startswith("data/"):
        return settings.project_root / s
    p = Path(s)
    return p if p.is_absolute() else None


def _relocate_into(path: Optional[Path], target_dir: Path) -> Optional[Path]:
    """把 outputs 下的图片移进项目目录；返回新路径，未移动则 None。

    安全边界：只动 `outputs/` 之下的文件；已在目标目录里的跳过；
    目标已存在同名文件时跳过 —— 绝不覆盖、绝不删除。
    老版本按 project_id 命名的 `comics/<project_id>/` 也在搬迁范围内
    （目录名即项目 id，归属明确）。
    """
    if path is None:
        return None
    try:
        src = path.resolve()
        target = target_dir.resolve()
        if not src.is_file():
            return None
        if src.parent == target:
            return None                      # 已经在该项目目录里
        src.relative_to(settings.output_dir_path.resolve())   # 不在 outputs 下 → 放弃
        dest = target / src.name
        if dest.exists():
            return None
        target.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        return dest
    except ValueError:
        return None                          # 不在 output_dir 之下，不动
    except Exception as exc:  # pragma: no cover - 防御性
        print(f"[comic] 图片归位失败（已跳过）{path}：{exc}")
        return None


def _resolve_existing(*values: Any) -> Optional[Path]:
    """按候选顺序解析路径 / URL，返回第一个**真实存在**的文件。

    跨机器拷贝的库里常存着指向旧机器盘符的绝对路径（如 `D:\\...`），
    这种路径解析出来是"真值"但文件不存在 —— 必须落到下一个候选
    （通常是 `/data/...` 静态 URL，在本机是有效的）。
    """
    for value in values:
        p = _resolve_stored_path(value)
        if p and p.is_file():
            return p
    return None


def _adopt_stored(record: dict, keys: tuple, target_dir: Path, update) -> bool:
    """把一条记录指向的图片归位到项目目录并修正记录（幂等）。

    `keys` 是该记录存路径的字段名（如 ('path', 'url') / ('image_url',)）。
    先按候选字段找一个真实存在的本机文件；文件已在项目目录里时只修数据库记录
    （跨机器拷库后记录常指向旧盘符），否则搬移文件再修记录。
    记录已指向本机文件时返回 False（幂等：不动作、不计数）。
    """
    src = _resolve_existing(*(record.get(k) for k in keys))
    if src is None or not src.is_file():
        return False
    try:
        target_resolved = Path(target_dir).resolve()
        primary = _resolve_stored_path(record.get(keys[0]))
        # 记录已正确指向项目目录里的本机文件 → 幂等跳过（不动作、不计数）
        if primary and primary.is_file() and primary.resolve().parent == target_resolved:
            return False
        if src.resolve().parent == target_resolved:
            update(src, relative_url(src))      # 已在位，只修记录
            return True
    except Exception as exc:  # pragma: no cover - 防御性
        print(f"[comic] 记录核对失败（已跳过）{src}：{exc}")
        return False
    dest = _relocate_into(src, target_dir)
    if not dest:
        return False
    update(dest, relative_url(dest))
    return True


def migrate_legacy_outputs() -> dict:
    """把历史出图归位到各自的项目目录（启动时执行，幂等）。

    早期版本所有出图都落在 `outputs/<日期>/`，多个项目的图混在一起。现在统一为
    `outputs/comics/<项目名>_<id尾号>/`，这里把**数据库里有明确归属**的历史图片搬过去，
    并同步更新数据库里的路径：角色立绘、场景概念图、分镜图、成品漫画页。
    主界面普通出图没有项目归属，保持原样不动。
    """
    moved = 0
    projects = crepo.list_projects()
    for project in projects:
        pid = project.get("id")
        if not pid:
            continue
        try:
            target = project_output_dir(project)
        except Exception as exc:  # pragma: no cover - 防御性
            print(f"[comic] 项目目录准备失败 {pid}：{exc}")
            continue

        for ch in crepo.list_characters(pid):
            ok = _adopt_stored(
                ch, ("ref_path", "ref_url"), target,
                lambda p, u, _id=ch["id"]: crepo.update_character(_id, ref_path=stored_path(p), ref_url=u))
            moved += 1 if ok else 0

        for sc in crepo.list_scenes(pid):
            ok = _adopt_stored(
                sc, ("image_url",), target,
                lambda p, u, _id=sc["id"]: crepo.update_scene(_id, image_url=u))
            moved += 1 if ok else 0

        for pn in crepo.list_panels(pid):
            ok = _adopt_stored(
                pn, ("image_url",), target,
                lambda p, u, _id=pn["id"]: crepo.update_panel(_id, image_url=u))
            moved += 1 if ok else 0

        for rd in crepo.list_renders(pid, limit=1000):
            ok = _adopt_stored(
                rd, ("path", "url"), target,
                lambda p, u, _id=rd["id"]: crepo.update_render(_id, path=stored_path(p), url=u))
            moved += 1 if ok else 0

        # 老版本的项目目录（comics/<project_id>）：搬空后删掉空目录，避免新旧目录并存。
        # 只删空目录 —— 里面还有东西（例如已删除项目留下的图）就原样保留。
        legacy_dir = settings.output_dir_path / "comics" / pid
        try:
            if legacy_dir.is_dir() and not any(legacy_dir.iterdir()):
                legacy_dir.rmdir()
        except Exception:  # pragma: no cover - 防御性
            pass

    if moved:
        print(f"[comic] 历史出图已按项目归位：{moved} 张")
    return {"moved": moved}


def normalize_stored_paths() -> int:
    """把历史库存的绝对路径改写成 `data/...` 相对路径（启动时执行，幂等）。

    存绝对路径是数据迁移的最大障碍（换电脑 / 换盘符后全部失效），
    统一为相对路径后，整个 data 目录拷到哪儿都能用。
    只处理项目根之下的文件；根之外的（异常情况）保持原样。
    """
    from app.db import db as _db

    root = settings.project_root
    fixed = 0

    def _relativize(value):
        if not value:
            return None
        p = _resolve_stored_path(value)
        if not p or not p.is_file():
            return None                     # 路径本来就坏了，交给迁移归位处理
        try:
            p.resolve().relative_to(root.resolve())
        except ValueError:
            return None                     # 项目根之外，保持原样
        rel = stored_path(p)
        return None if str(value).replace("\\", "/") == rel else rel

    with _db.write() as conn:
        for table, col, extra in (("images", "path", "url"),
                                  ("comic_renders", "path", "url"),
                                  ("comic_characters", "ref_path", "ref_url")):
            rows = conn.execute(
                f"SELECT id, {col} AS v, {extra} AS alt FROM {table} WHERE {col} IS NOT NULL"
            ).fetchall()
            for r in rows:
                # 主字段找不到文件时（跨机器拷库的典型情况）用 URL 兜底再试一次
                new_val = _relativize(r["v"]) or _relativize(r["alt"])
                if new_val:
                    conn.execute(f"UPDATE {table} SET {col} = ? WHERE id = ?", (new_val, r["id"]))
                    fixed += 1

    if fixed:
        print(f"[comic] 素材路径已改为相对路径：{fixed} 条")
    return fixed


def _characters_for(panel: dict, characters: list[dict]) -> list[dict]:
    try:
        ids = json.loads(panel.get("character_ids") or "[]")
    except Exception:
        ids = []
    if not ids:
        return []
    by_id = {c["id"]: c for c in characters}
    return [by_id[i] for i in ids if i in by_id]


def build_prompt(project: dict, panel: dict, characters: list[dict],
                 ref_slots: Optional[list[dict]] = None) -> tuple[str, str]:
    """拼装单格提示词与负面提示词。

    ref_slots：本格实际挂上的参考图编号（decide_panel_refs 的产物）。
      * 为空 → 纯文生图措辞，绝不出现 "image_1 / reference"（没图却提图会凭空造一张立绘）；
      * 有图 → 按 Qwen-Image 2.1 官方编辑用法，用 image_1 / image_2 指代参考图，
        并明确"人物要自然融入场景"，而不是把参考图并排贴出来。
    """
    style = resolve_style(project)
    shot = SHOT_PRESETS.get(panel.get("shot") or "medium", SHOT_PRESETS["medium"])

    slots = list(ref_slots or [])
    scene_slot = next((s["slot"] for s in slots if s["kind"] == "scene"), None)
    char_slots = [s for s in slots if s["kind"] == "portrait"]
    char_ids = {s["name"]: s["slot"] for s in char_slots}

    parts: list[str] = []
    if project.get("keep_style", 1):
        parts.append(style.get("prompt_text") or "")
        parts.append("single comic panel, consistent art style across the whole comic")
    parts.append(shot["en"])

    # 主题色：一行文本注入（handraw 机制），显存零压力，还能外溢到排版配色
    theme_line = theme_prompt_text(project)
    if theme_line:
        parts.append(theme_line)

    scene = (panel.get("scene") or "").strip()

    # 该格所属场景的设定（场景提取产物）会作为环境锚点注入，保证同一场景前后一致
    scene_row = crepo.get_scene(panel["scene_id"]) if panel.get("scene_id") else None
    if scene_row:
        env = "；".join(x for x in (
            scene_row.get("location"), scene_row.get("time_of_day"),
            scene_row.get("atmosphere"), scene_row.get("desc"),
        ) if x)
        if env:
            parts.append(f"location setting: {env}")
        if scene_row.get("prompt"):
            parts.append(f"background reference: {scene_row['prompt'].strip()}")

    if scene:
        parts.append(f"scene: {scene}")

    selected = _characters_for(panel, characters)
    for c in selected:
        bits = [f"character {c['name']}"]
        # 优先用角色提取产出的英文细节描述，绘图一致性最好
        if c.get("detail_prompt"):
            bits.append(c["detail_prompt"].strip())
        if c.get("appearance"):
            bits.append(c["appearance"].strip())
        if c.get("outfit"):
            bits.append(f"wearing {c['outfit'].strip()}")
        if c.get("personality"):
            bits.append(f"personality {c['personality'].strip()}")
        parts.append(", ".join(b for b in bits if b))

    # ---- 融合指令（Qwen-Image 2.1 编辑用法：用 image_N 指代参考图）----
    # 必须有角色参考才发融合指令：只有场景图时发"把角色放进 image_1"只会
    # 换来一张原样复刻的空背景（上游 decide_panel_refs 已保证不会走到这）
    if any(s["kind"] == "portrait" for s in slots):
        refs_txt = ", ".join(f"{s['slot']} is {s['name']}"
                             for s in slots if s["kind"] == "portrait")
        if scene_slot:
            task = (f"draw one new comic panel ({shot['en']}): place the character(s) "
                    f"from {refs_txt} into the environment shown in {scene_slot}")
        else:
            task = (f"draw one new comic panel ({shot['en']}) with the character(s) "
                    f"from {refs_txt}, in a completely new composition and background")
        parts.append(
            f"Task: {task}. "
            "Keep every character's face, hairstyle and outfit exactly the same as their "
            "reference image so the identity stays consistent. "
            + ("Keep the environment, architecture, lighting and color palette of "
               f"{scene_slot}, and keep the same overall composition and camera framing it shows. "
               if scene_slot else "")
            + "The characters must be naturally integrated into the scene: correct scale, "
              "perspective, contact shadows and light direction, interacting with the "
              "environment. Redraw everything as one single coherent illustration — "
              "do not paste, frame or duplicate the reference images, "
              "no side-by-side portrait layout, no collage, no split panels, no borders."
        )

    dialogue = (panel.get("dialogue") or "").strip()
    if dialogue:
        # 对白要影响画面（表情与口型），但文字本身由排版阶段绘制，不能让模型写字
        parts.append(f"depicting this moment of dialogue (do not draw any text): {dialogue}")

    extra = (panel.get("extra_prompt") or "").strip()
    if extra:
        parts.append(extra)

    parts.append(ANATOMY_POSITIVE)
    parts.append("clean composition, readable staging, high detail")

    prompt = ", ".join(p for p in parts if p)

    # 负面提示词叠加顺序：画面通用 → 分镜禁字 → 人物专用 → 角色个性补充
    # 注意：融合模式走官方 2.1 路径（CFG=1），负面提示词不参与采样，此处仅作记录
    base_neg = (project.get("negative") or "").strip() or DEFAULT_PROJECT_NEGATIVE
    neg_parts = [base_neg, PANEL_NEGATIVE_EXTRA]
    if selected:
        neg_parts.append((project.get("char_negative") or "").strip() or DEFAULT_CHAR_NEGATIVE)
        for c in selected:
            if (c.get("negative") or "").strip():
                neg_parts.append(c["negative"].strip())
    negative = ", ".join(x for x in neg_parts if x)
    return prompt, negative


# ============================================================
#  分镜生成
# ============================================================


class ComicError(RuntimeError):
    """漫画业务错误，message 直接面向用户。"""


# 拼合参考图时的占位底色（中性浅灰，与角色立绘的纯色背景接近）
_PANEL_REF_BG = (206, 208, 213)
# 单格最多挂几张参考图（6GB 显存：每张参考图都要进视觉塔 + VAE latent，越多越慢）
_MAX_FUSION_REFS = 4
# 角色参考图长边上限：参考图 latent 直接计入采样序列长度，
# 立绘缩到 512 以内几乎不影响"看得清脸"，但每步耗时能降下来
_CHAR_REF_MAX_SIDE = 512


def _cover_fit(img: Image.Image, width: int, height: int) -> Image.Image:
    """等比缩放到刚好覆盖目标框，再从中心裁掉多余部分。"""
    if img.width <= 0 or img.height <= 0:
        return Image.new("RGB", (width, height), _PANEL_REF_BG)
    scale = max(width / img.width, height / img.height)
    new = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                     Image.LANCZOS)
    left = max(0, (new.width - width) // 2)
    top = max(0, (new.height - height) // 2)
    return new.crop((left, top, left + width, top + height))


def _load_ref_image(char: dict) -> Optional[Image.Image]:
    p = resolve_stored(char.get("ref_path"))
    if not p or not p.exists():
        return None
    try:
        return Image.open(p).convert("RGB")
    except Exception:
        return None


def _limit_side(img: Image.Image, max_side: int) -> Image.Image:
    """只缩小不放大，长边限制到 max_side 并取 32 的整数倍（VAE 下采样要求）。

    总是返回**新对象**（无需缩放时也复制一份）：调用方随后会 close 原图，
    返回同一对象会导致"操作已关闭的图片"。
    """
    longest = max(img.width, img.height)
    if longest <= max_side:
        return img.copy()
    scale = max_side / longest
    w = max(64, int(round(img.width * scale / 32)) * 32)
    h = max(64, int(round(img.height * scale / 32)) * 32)
    return img.resize((w, h), Image.LANCZOS)


def _pad_to_canvas(img: Image.Image, width: int, height: int,
                   bg: tuple[int, int, int] = _PANEL_REF_BG) -> Image.Image:
    """等比缩放到能放进画布，居中放置，四周补中性底色（不裁切、不拉伸）。

    Qwen-Image 2.1 编辑时输出尺寸跟随 image_1，所以没有场景图时
    用首张立绘"装进"目标画布来锁定出图尺寸。
    """
    scale = min(width / max(1, img.width), height / max(1, img.height), 1.0)
    new = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                     Image.LANCZOS)
    canvas = Image.new("RGB", (width, height), bg)
    canvas.paste(new, ((width - new.width) // 2, (height - new.height) // 2))
    return canvas


def _resolve_scene_image(scene: Optional[dict]) -> Optional[Image.Image]:
    """加载已生成的场景概念图（优先图片记录的绝对路径，其次静态 URL）。"""
    if not scene:
        return None
    path: Optional[Path] = None
    image_id = scene.get("image_id")
    if image_id:
        try:
            from app import repo

            record = repo.get_image(image_id)
            if record and record.get("path"):
                cand = resolve_stored(record["path"])
                path = cand if cand and cand.exists() else None
        except Exception:
            path = None
    if path is None:
        url = scene.get("image_url") or ""
        if url:
            cand = Path(url)
            if not cand.is_absolute():
                cand = settings.project_root / url.lstrip("/\\")
            path = cand if cand.exists() else None
    if path is None:
        return None
    try:
        return Image.open(path).convert("RGB")
    except Exception:
        return None


def decide_panel_refs(project: dict, panel: dict, characters: list[dict],
                      load: bool = True) -> dict:
    """决定单格分镜的参考图构成（enqueue 与「词」面板共用同一口径）。

    **走的是 Qwen-Image 2.1 官方多参考图编辑通道**（引擎侧对应
    TextEncodeQwenImage21）：参考图既进 Qwen3-VL 视觉塔、又作为 VAE latent
    拼进序列，所以模型是"一边看图一边按指令画"，而不是把参考图当底稿复刻。

    为什么不能用图生图拼贴（踩过的两个坑都源于此）：
      * 立绘铺满画布 + 低去噪 → 出图就是人物图片拼接，场景全无；
      * 场景图铺满画布 + 低去噪 → 出图只有场景，人物进不来。
    低去噪图生图的构图由参考图主导，物理上就不可能"融合"。

    参考图顺序即提示词里的编号，image_1 决定输出尺寸：
      * 有场景概念图 → image_1 = 场景图（铺满项目尺寸），其后逐个角色立绘；
      * 没有场景图   → image_1 = 首个角色立绘（装入项目画布），其后其余角色。

    **必须有至少一张角色立绘才走融合**：只有场景图时（典型场景：换电脑后
    立绘的绝对路径失效）模型拿到"保持 image_1 构图"的指令却没有任何人物
    参考，会原样复刻背景、人物根本进不来 —— 这正是"只有背景没有人物"的
    另一诱因。此时退回 txt2img，人物由提示词文字描述生成。
    返回 {"mode", "refs", "slots", "ref_kind"}；load=False 只算口径不加载图。
    """
    selected = _characters_for(panel, characters)
    empty = {"mode": "txt2img", "refs": [], "slots": [], "ref_kind": None}
    if not (project.get("use_ref", 1) and selected):
        return empty

    width, height = int(project["width"]), int(project["height"])
    scene = crepo.get_scene(panel["scene_id"]) if panel.get("scene_id") else None
    scene_ok = bool(scene and scene.get("status") == "done")
    char_refs = [c for c in selected
                 if resolve_stored(c.get("ref_path")) and resolve_stored(c.get("ref_path")).exists()]
    scene_name = (scene or {}).get("name") or "场景"

    if not char_refs:
        # 没有任何可用立绘：绝不做"纯场景"融合（会复刻空背景），退回文生图
        return empty

    if not load:
        # 只出决策口径（「词」面板用）：编号按"场景优先"推定，不加载图片
        slots: list[dict] = []
        if scene_ok:
            slots.append({"slot": "image_1", "kind": "scene", "name": scene_name})
            rest = char_refs
        else:
            slots.append({"slot": "image_1", "kind": "portrait", "name": char_refs[0]["name"]})
            rest = char_refs[1:]
        for c in rest[:_MAX_FUSION_REFS]:
            slots.append({"slot": f"image_{len(slots) + 1}", "kind": "portrait",
                          "name": c["name"]})
        kind = "scene+portrait" if (scene_ok and char_refs) else (
            "portrait" if char_refs else "scene")
        return {"mode": "fusion", "refs": [], "slots": slots, "ref_kind": kind}

    # ---- 真正组装参考图：编号与实际 refs 严格一一对应，绝不靠截断对齐 ----
    refs: list[Image.Image] = []
    slots = []
    if scene_ok:
        img = _resolve_scene_image(scene)
        if img is not None:
            refs.append(_cover_fit(img, width, height))  # 环境铺满画布，同时锁定输出尺寸
            img.close()
            slots.append({"slot": "image_1", "kind": "scene", "name": scene_name})

    for c in char_refs[:_MAX_FUSION_REFS]:
        img = _load_ref_image(c)
        if img is None:
            continue
        if not refs:
            # 没有可用场景图：首张立绘装进项目画布当 image_1，锁定输出尺寸
            refs.append(_pad_to_canvas(img, width, height))
        else:
            refs.append(_limit_side(img, _CHAR_REF_MAX_SIDE))  # 立绘保持比例，长边限缩
        img.close()
        slots.append({"slot": f"image_{len(refs)}", "kind": "portrait", "name": c["name"]})

    if not refs:
        return empty

    has_scene = any(s["kind"] == "scene" for s in slots)
    has_portrait = any(s["kind"] == "portrait" for s in slots)
    kind = "scene+portrait" if (has_scene and has_portrait) else (
        "portrait" if has_portrait else "scene")
    return {"mode": "fusion", "refs": refs, "slots": slots, "ref_kind": kind}


def _panel_seed(panel: dict, selected: list[dict], reseed: bool) -> tuple[int, bool]:
    """决定这一格用哪个种子。

    返回 (seed, locked)：
      * reseed=True（重绘默认）→ **每次换随机种子**，否则同 prompt + 同 seed
        必然出同一张图（这正是「重绘多次效果一样」的根因）；
      * reseed=False（勾了「锁定种子」）→ 复用该格上次的种子；没有则退回
        第一个角色的固定种子，便于复现与微调。
    """
    if not reseed:
        cur = int(panel.get("seed") or -1)
        if cur >= 0:
            return cur, True
        for c in selected:
            s = int(c.get("seed") or -1)
            if s >= 0:
                return s, True
        return _rand_seed(), True
    return _rand_seed(), False


async def enqueue_panel(panel_id: str, strength: float | None = None,
                        reseed: bool = True) -> dict:
    """把单个分镜提交到出图队列（串行执行，复用主调度器）。

    strength 为 None 时取项目设置 ref_strength（默认 0.55，越低越像形象图）。
    reseed 见 _panel_seed：默认换种子，保证「重绘」能出不同的图。
    """
    from app.services import generation
    from app.services.generation import GenerationError

    panel = crepo.get_panel(panel_id)
    if not panel:
        raise ComicError("分镜不存在")
    project = crepo.get_project(panel["project_id"])
    if not project:
        raise ComicError("项目不存在")

    characters = crepo.list_characters(project["id"])

    # 参考图构成必须先定：提示词里的 image_1/image_2 编号取自它
    ref = decide_panel_refs(project, panel, characters, load=True)
    prompt, negative = build_prompt(project, panel, characters, ref_slots=ref["slots"])

    selected = _characters_for(panel, characters)
    seed, locked = _panel_seed(panel, selected, reseed)

    # 通道诊断日志：在另一台机器排查"只有背景/只有人物"时，看后端窗口这行就够了
    if ref["refs"]:
        kinds = "+".join(sorted({s["kind"] for s in ref["slots"]}))
        print(f"[comic] 分镜#{panel.get('seq')} {panel.get('shot')} → 2.1融合："
              f"{len(ref['refs'])} 张参考图（{kinds}），CFG=1", flush=True)
    else:
        if not project.get("use_ref", 1):
            reason = "项目设置关闭了形象参考"
        elif not selected:
            reason = "该格未绑定角色"
        else:
            reason = "角色立绘文件缺失或未生成（检查角色卡的头像）"
        print(f"[comic] 分镜#{panel.get('seq')} {panel.get('shot')} → 文生图（{reason}）",
              flush=True)

    payload: dict[str, Any] = {
        "job_id": new_id("job"),
        "prompt": prompt,
        "negative_prompt": negative,
        "width": int(project["width"]),
        "height": int(project["height"]),
        "steps": int(project["steps"]),
        # 融合走 Qwen-Image 2.1 官方路径：CFG 固定 1（此时负面提示词不参与采样）
        "guidance_scale": 1.0 if ref["refs"] else float(project["guidance"]),
        "seed": seed,
        "batch_size": 1,
        # 候选张数：16GB 下 batch 2~4 一次出多张挑（本机默认 1，避免机时翻倍）
        "count": max(1, int(project.get("batch") or 1)),
        "mode": "txt2img",
        "strength": 1.0,
        "channel": "auto",
        "comic_panel_id": panel_id,
        "comic_project_id": project["id"],
        # 按项目归档：outputs/comics/<项目名_id尾号>/
        "output_subdir": project_output_subdir(project),
    }

    if ref["refs"]:
        data_urls: list[str] = []
        for img in ref["refs"]:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            img.close()
            data_urls.append("data:image/png;base64," + base64.b64encode(buf.getvalue()).decode())
        payload["mode"] = "img2img"          # 复用主队列的图生图通道
        payload["fusion"] = True             # 标记：走 2.1 多参考图编辑（TextEncodeQwenImage21）
        payload["image"] = data_urls[0]      # image_1 决定输出尺寸
        payload["ref_images"] = data_urls
        payload["ref_slots"] = ref["slots"]

    crepo.update_panel(panel_id, prompt=prompt, status="queued", error=None, seed=seed)

    try:
        result = await generation.submit_batch(payload)
    except GenerationError as exc:
        # 队列已满等情况：不算失败，回到草稿态并把原因留给用户看
        crepo.update_panel(panel_id, status="draft", error=str(exc))
        raise ComicError(str(exc)) from exc

    crepo.update_panel(panel_id, job_id=payload["job_id"])
    return {"panel_id": panel_id, "job_id": payload["job_id"],
            "seed": seed, "seed_locked": locked,
            "ref_kind": ref["ref_kind"], "ref_count": len(ref["refs"]), **result}


async def enqueue_project(project_id: str, only_missing: bool = True,
                          strength: float | None = None,
                          reseed: bool = True) -> dict:
    """批量提交整部漫画的分镜。"""
    panels = crepo.list_panels(project_id)
    if not panels:
        raise ComicError("还没有分镜，请先解析剧本")

    targets = [p for p in panels if (not only_missing or p["status"] != "done")]
    if not targets:
        return {"submitted": 0, "failed": 0, "panel_ids": [],
                "message": "所有分镜都已生成，可单格重绘"}

    submitted: list[str] = []
    failed: list[str] = []
    queue_full = False
    for p in targets:
        try:
            await enqueue_panel(p["id"], strength=strength, reseed=reseed)
            submitted.append(p["id"])
        except ComicError as exc:
            failed.append(p["id"])
            if "队列已满" in str(exc):
                queue_full = True
                break
    message = f"已提交 {len(submitted)} 格"
    if queue_full:
        message += "（队列已满，其余分镜请稍后再提交一次）"
    return {
        "submitted": len(submitted),
        "failed": len(failed),
        "panel_ids": submitted,
        "queue_full": queue_full,
        "message": message,
    }


def _retry(fn, attempts: int = 4, delay: float = 0.35) -> None:
    """写库偶发失败时的短重试（并发写入保护）。"""
    import time as _time

    last: Exception | None = None
    for i in range(attempts):
        try:
            fn()
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            _time.sleep(delay * (i + 1))
    if last:
        raise last


def on_panel_image(panel_id: str, record: dict) -> None:
    """出图成功的回调（由 generation.run_job 触发）。"""
    _retry(lambda: crepo.update_panel(
        panel_id,
        status="done",
        image_id=record.get("id"),
        image_url=record.get("url"),
        seed=record.get("seed"),
        error=None,
    ))


def on_panel_error(panel_id: str, message: str) -> None:
    """出图失败的回调。"""
    _retry(lambda: crepo.update_panel(panel_id, status="failed", error=(message or "生成失败")[:400]))


# ============================================================
#  场景概念图（环境空镜，无人物）
# ============================================================

def build_scene_prompt(project: dict, scene: dict) -> tuple[str, str]:
    """场景概念图提示词：环境全景、无人物，作为整部漫画的背景锚点。"""
    style_text = style_prompt_text(project)
    parts: list[str] = []
    if style_text:
        parts.append(style_text)
    parts.append("background concept art, environment only, no people")
    theme_line = theme_prompt_text(project)
    if theme_line:
        parts.append(theme_line)
    if scene.get("prompt"):
        parts.append(scene["prompt"].strip())
    detail = "；".join(x for x in (
        scene.get("location"), scene.get("time_of_day"),
        scene.get("atmosphere"), scene.get("desc"),
    ) if x)
    if detail:
        parts.append(f"location setting: {detail}")
    parts.append("establishing wide view, clear architecture and props, cinematic lighting, high detail")

    prompt = ", ".join(p for p in parts if p)
    negative = ", ".join(x for x in [
        (project.get("negative") or "").strip() or DEFAULT_PROJECT_NEGATIVE,
        SCENE_NEGATIVE_EXTRA,
    ] if x)
    return prompt, negative


async def enqueue_scene_image(scene_id: str) -> dict:
    """把场景概念图提交到主出图队列。"""
    from app.services import generation
    from app.services.generation import GenerationError

    scene = crepo.get_scene(scene_id)
    if not scene:
        raise ComicError("场景不存在")
    project = crepo.get_project(scene["project_id"])
    if not project:
        raise ComicError("项目不存在")

    prompt, negative = build_scene_prompt(project, scene)
    # 场景概念图用横构图更接近漫画背景的视野
    width, height = int(project["width"]), int(project["height"])
    if width == height:
        width, height = height, max(512, int(height * 3 / 4))

    payload: dict[str, Any] = {
        "job_id": new_id("job"),
        "prompt": prompt,
        "negative_prompt": negative,
        "width": width,
        "height": height,
        "steps": int(project["steps"]),
        "guidance_scale": float(project["guidance"]),
        "seed": int(scene.get("seed") if scene.get("seed") is not None else -1),
        "batch_size": 1,
        "count": 1,
        "mode": "txt2img",
        "strength": 0.6,
        "channel": "auto",
        "comic_scene_id": scene_id,
        "comic_project_id": project["id"],
        "output_subdir": project_output_subdir(project),
    }
    # 注意：拼装结果写到 last_prompt，绝不能回写 prompt 字段 ——
    # build_scene_prompt 会读 prompt 作为输入，回写会造成每生成一次就套一层包装。
    crepo.update_scene(scene_id, last_prompt=prompt, status="queued", error=None)
    try:
        result = await generation.submit_batch(payload)
    except GenerationError as exc:
        crepo.update_scene(scene_id, status="draft", error=str(exc))
        raise ComicError(str(exc)) from exc

    crepo.update_scene(scene_id, job_id=payload["job_id"])
    return {"scene_id": scene_id, "job_id": payload["job_id"], **result}


async def enqueue_project_scenes(project_id: str, only_missing: bool = True) -> dict:
    scenes = crepo.list_scenes(project_id)
    if not scenes:
        raise ComicError("还没有场景，请先在「场景」步骤提取场景")
    targets = [s for s in scenes if (not only_missing or s["status"] != "done")]
    if not targets:
        return {"submitted": 0, "failed": 0, "message": "所有场景图都已生成"}

    submitted, failed, queue_full = [], [], False
    for s in targets:
        try:
            await enqueue_scene_image(s["id"])
            submitted.append(s["id"])
        except ComicError as exc:
            failed.append(s["id"])
            if "队列已满" in str(exc):
                queue_full = True
                break
    message = f"已提交 {len(submitted)} 个场景"
    if queue_full:
        message += "（队列已满，其余请稍后再提交）"
    return {"submitted": len(submitted), "failed": len(failed),
            "scene_ids": submitted, "queue_full": queue_full, "message": message}


def on_scene_image(scene_id: str, record: dict) -> None:
    _retry(lambda: crepo.update_scene(
        scene_id,
        status="done",
        image_id=record.get("id"),
        image_url=record.get("url"),
        seed=record.get("seed"),
        error=None,
    ))


def on_scene_error(scene_id: str, message: str) -> None:
    _retry(lambda: crepo.update_scene(scene_id, status="failed", error=(message or "生成失败")[:400]))


# ============================================================
#  角色形象图（本地模型自绘，作为整部漫画的角色锚点）
#  形象图生成后即成为该角色的图生图参考 —— 全流程无需任何手动上传。
# ============================================================

def _rand_seed() -> int:
    return random.randint(1, 2 ** 31 - 1)


def build_character_prompt(project: dict, character: dict) -> tuple[str, str]:
    """角色形象图提示词：单人半身立绘，中性背景，突出可复用的形象特征。

    刻意做成**半身像**而不是全身设定图：分镜绝大多数是中景/近景，
    用半身像做图生图参考时脸部占比更大，形象（尤其是五官）传导更准；
    全身小人的话脸部只有几十像素，图生图时根本锁不住长相。
    """
    style_text = style_prompt_text(project)
    parts: list[str] = []
    if style_text:
        parts.append(style_text)
    theme_line = theme_prompt_text(project)
    if theme_line:
        parts.append(theme_line)
    parts.append("single character reference, exactly one person only, "
                 "upper body portrait, waist up, front view facing camera, "
                 "plain neutral gray background, clear detailed face, "
                 "centered composition, character design reference")

    bits = [f"character {character.get('name') or 'unnamed'}"]
    # 优先用角色提取产出的英文细节描述，跨格一致性最好
    if character.get("detail_prompt"):
        bits.append(character["detail_prompt"].strip())
    if character.get("gender"):
        bits.append(str(character["gender"]).strip())
    if character.get("age"):
        bits.append(str(character["age"]).strip())
    if character.get("appearance"):
        bits.append(character["appearance"].strip())
    if character.get("outfit"):
        bits.append(f"wearing {character['outfit'].strip()}")
    if character.get("personality"):
        bits.append(f"personality {character['personality'].strip()}")
    if len(bits) > 1:
        parts.append(", ".join(b for b in bits if b))

    background = (character.get("background") or "").strip()
    if background:
        parts.append(f"character background hint: {background}")

    parts.append(ANATOMY_POSITIVE)
    parts.append("calm neutral natural expression, looking at camera, "
                 "clear face and silhouette, distinctive memorable design, "
                 "consistent identity across panels, high detail")

    prompt = ", ".join(p for p in parts if p)
    negative = ", ".join(x for x in [
        (project.get("negative") or "").strip() or DEFAULT_PROJECT_NEGATIVE,
        (project.get("char_negative") or "").strip() or DEFAULT_CHAR_NEGATIVE,
        CHARACTER_NEGATIVE_EXTRA,
        (character.get("negative") or "").strip(),
    ] if x)
    return prompt, negative


async def enqueue_character_image(character_id: str) -> dict:
    """把角色形象图提交到主出图队列（本地模型自绘）。"""
    from app.services import generation
    from app.services.generation import GenerationError

    char = crepo.get_character(character_id)
    if not char:
        raise ComicError("角色不存在")
    project = crepo.get_project(char["project_id"])
    if not project:
        raise ComicError("项目不存在")

    prompt, negative = build_character_prompt(project, char)

    # 形象图用方构图
    side = min(int(project["width"]), int(project["height"]))
    side = max(640, min(1024, side))

    # 固定种子：首次生成时定下，之后重绘与分镜都复用它，形象才稳定
    seed = int(char.get("seed")) if char.get("seed") is not None else -1
    if seed < 0:
        seed = _rand_seed()
        crepo.update_character(character_id, seed=seed)

    payload: dict[str, Any] = {
        "job_id": new_id("job"),
        "prompt": prompt,
        "negative_prompt": negative,
        "width": side,
        "height": side,
        "steps": int(project["steps"]),
        "guidance_scale": float(project["guidance"]),
        "seed": seed,
        "batch_size": 1,
        "count": 1,
        "mode": "txt2img",
        "strength": 0.6,
        "channel": "auto",
        "comic_character_id": character_id,
        "comic_project_id": project["id"],
        "output_subdir": project_output_subdir(project),
    }
    crepo.update_character(character_id, prompt=prompt, status="queued", error=None)
    try:
        result = await generation.submit_batch(payload)
    except GenerationError as exc:
        crepo.update_character(character_id, status="draft", error=str(exc))
        raise ComicError(str(exc)) from exc

    crepo.update_character(character_id, job_id=payload["job_id"])
    return {"character_id": character_id, "job_id": payload["job_id"], "seed": seed, **result}


async def enqueue_characters(project_id: str, only_missing: bool = True) -> dict:
    """批量生成整部漫画的角色形象图。"""
    chars = crepo.list_characters(project_id)
    if not chars:
        raise ComicError("还没有角色，请先提取角色")
    targets = [c for c in chars if (not only_missing or c.get("status") != "done")]
    if not targets:
        return {"submitted": 0, "failed": 0, "message": "所有角色都已有形象图"}

    submitted, failed = 0, 0
    for c in targets:
        try:
            await enqueue_character_image(c["id"])
            submitted += 1
        except ComicError:
            failed += 1
    return {"submitted": submitted, "failed": failed,
            "message": f"已提交 {submitted} 个角色形象图" + (f"，{failed} 个失败" if failed else "")}


def on_character_image(character_id: str, record: dict) -> None:
    """形象图出图成功：直接落为该角色的图生图参考，后续分镜自动沿用。"""
    _retry(lambda: crepo.update_character(
        character_id,
        status="done",
        image_id=record.get("id"),
        image_url=record.get("url"),
        ref_path=stored_path(record.get("path") or ""),
        ref_url=record.get("url"),
        ref_source="local",
        seed=record.get("seed"),
        error=None,
    ))


def on_character_error(character_id: str, message: str) -> None:
    _retry(lambda: crepo.update_character(
        character_id, status="failed", error=(message or "生成失败")[:400]))


def reconcile_project(project_id: str) -> int:
    """按任务实际状态校正分镜 / 角色形象图状态（回写丢失时自愈）。返回修正条数。"""
    from app import repo

    fixed = 0
    for char in crepo.list_characters(project_id):
        if char.get("status") not in ("queued", "running"):
            continue
        job_id = char.get("job_id")
        job = repo.get_job(job_id) if job_id else None
        if not job:
            crepo.update_character(char["id"], status="draft" if not char.get("ref_path") else "done")
            fixed += 1
            continue
        if job.get("status") == "succeeded":
            images = repo.images_by_job(job_id)
            if images:
                on_character_image(char["id"], images[-1])
                fixed += 1
            else:
                crepo.update_character(char["id"], status="draft", error="任务成功但未取回图片")
                fixed += 1
        elif job.get("status") in ("failed", "canceled"):
            crepo.update_character(char["id"], status="failed",
                                   error=(job.get("error") or "生成失败")[:400])
            fixed += 1

    for panel in crepo.list_panels(project_id):
        if panel.get("status") not in ("queued", "running"):
            continue
        job_id = panel.get("job_id")
        if not job_id:
            crepo.update_panel(panel["id"], status="draft")
            fixed += 1
            continue
        job = repo.get_job(job_id)
        if not job:
            crepo.update_panel(panel["id"], status="draft")
            fixed += 1
            continue
        if job.get("status") == "succeeded":
            images = repo.images_by_job(job_id)
            if images:
                crepo.update_panel(panel["id"], status="done", image_id=images[-1]["id"],
                                   image_url=images[-1]["url"], seed=images[-1]["seed"], error=None)
                fixed += 1
            else:
                crepo.update_panel(panel["id"], status="draft", error="任务成功但未取回图片")
                fixed += 1
        elif job.get("status") in ("failed", "canceled"):
            crepo.update_panel(panel["id"], status="failed",
                               error=(job.get("error") or "生成失败")[:400])
            fixed += 1

    # 场景概念图：与分镜同构。漏掉这一段时，场景一旦进入 queued
    # （任务被取消 / 服务重启丢队列 / 回写失败）就永远卡住不会恢复，
    # 界面表现为「场景图一直生不出来」。
    for scene in crepo.list_scenes(project_id):
        if scene.get("status") not in ("queued", "running"):
            continue
        job_id = scene.get("job_id")
        if not job_id:
            crepo.update_scene(scene["id"], status="draft")
            fixed += 1
            continue
        job = repo.get_job(job_id)
        if not job:
            crepo.update_scene(scene["id"], status="draft")
            fixed += 1
            continue
        if job.get("status") == "succeeded":
            images = repo.images_by_job(job_id)
            if images:
                crepo.update_scene(scene["id"], status="done", image_id=images[-1]["id"],
                                   image_url=images[-1]["url"], seed=images[-1]["seed"],
                                   error=None)
            else:
                crepo.update_scene(scene["id"], status="draft", error="任务成功但未取回图片")
            fixed += 1
        elif job.get("status") in ("failed", "canceled"):
            crepo.update_scene(scene["id"], status="failed",
                               error=(job.get("error") or "生成失败")[:400])
            fixed += 1
    return fixed


# ============================================================
#  排版渲染
# ============================================================

_FONT_CANDIDATES = [
    r"C:/Windows/Fonts/msyh.ttc",
    r"C:/Windows/Fonts/msyhl.ttc",
    r"C:/Windows/Fonts/simhei.ttf",
    r"C:/Windows/Fonts/simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]
_FONT_BOLD_CANDIDATES = [
    r"C:/Windows/Fonts/msyhbd.ttc",
    r"C:/Windows/Fonts/simhei.ttf",
    *([]),
]

_font_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    for path in (_FONT_BOLD_CANDIDATES if bold else _FONT_CANDIDATES):
        if Path(path).exists():
            try:
                font = ImageFont.truetype(path, size)
                _font_cache[key] = font
                return font
            except Exception:
                continue
    font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def _wrap_cjk(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """中英混排换行（按绘制宽度累积）。"""
    lines: list[str] = []
    for paragraph in (text or "").split("\n"):
        if not paragraph:
            continue
        cur = ""
        for ch in paragraph:
            trial = cur + ch
            if font.getlength(trial) <= max_width or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = ch
        if cur:
            lines.append(cur)
    return lines or [""]


def _draw_bubble(page: Image.Image, box: tuple[int, int, int, int], text: str,
                 position: str = "auto", index: int = 0, font_scale: float = 1.0,
                 ink: tuple[int, int, int] = (20, 20, 20)) -> None:
    """在指定格内绘制对话气泡（圆角矩形 + 尾巴 + 自动换行）。

    ink：气泡描边与文字颜色，跟随项目主题色（未设主题色时是近黑色）。
    """
    x0, y0, x1, y1 = box
    cell_w = x1 - x0
    cell_h = y1 - y0
    font_size = max(12, min(26, int(cell_w * 0.042 * font_scale)))
    font = _load_font(font_size)

    padding = max(10, int(cell_w * 0.035))
    max_text_w = int(cell_w * 0.72) - padding * 2
    lines = _wrap_cjk(text, font, max_text_w)
    line_h = int(font_size * 1.45)
    text_w = max((font.getlength(l) for l in lines), default=font_size)
    bubble_w = min(int(cell_w * 0.78), int(text_w) + padding * 2)
    bubble_h = min(int(cell_h * 0.6), line_h * len(lines) + padding * 2)

    if position == "top":
        anchor = "top"
    elif position == "bottom":
        anchor = "bottom"
    else:
        anchor = "top" if index % 2 else "bottom"

    bx0 = x0 + int((cell_w - bubble_w) / 2)
    if anchor == "top":
        by0 = y0 + int(cell_h * 0.05)
    else:
        by0 = y1 - bubble_h - int(cell_h * 0.06)
    bx1, by1 = bx0 + bubble_w, by0 + bubble_h

    shrink = 0
    while (by1 > y1 - 4 or by1 < y0 + 4) and shrink < 6:
        bubble_h = int(bubble_h * 0.86)
        by1 = by0 + bubble_h
        shrink += 1

    draw = ImageDraw.Draw(page)
    radius = max(8, int(min(bubble_w, bubble_h) * 0.22))
    draw.rounded_rectangle([bx0, by0, bx1, by1], radius=radius,
                           fill=(255, 255, 255), outline=ink, width=2)

    # 尾巴：朝下的三角（或朝上）
    tail_w = max(10, int(cell_w * 0.03))
    cx = bx0 + bubble_w // 2
    if anchor == "top":
        draw.polygon([(cx - tail_w, by1 - 2), (cx + tail_w // 2, by1 + tail_w), (cx + tail_w, by1 - 2)],
                     fill=(255, 255, 255), outline=ink)
    else:
        draw.polygon([(cx - tail_w, by0 + 2), (cx + tail_w // 2, by0 - tail_w), (cx + tail_w, by0 + 2)],
                     fill=(255, 255, 255), outline=ink)

    ty = by0 + padding
    for line in lines:
        lw = font.getlength(line)
        draw.text((bx0 + (bubble_w - lw) / 2, ty), line, font=font, fill=ink)
        ty += line_h


def _draw_sfx(page: Image.Image, box: tuple[int, int, int, int], text: str,
              ink: tuple[int, int, int] = (15, 15, 15)) -> None:
    x0, y0, x1, y1 = box
    cell_w, cell_h = x1 - x0, y1 - y0
    font = _load_font(max(18, int(cell_w * 0.13)), bold=True)
    w, h = font.getlength(text), font.size
    tx, ty = x1 - w - int(cell_w * 0.06), y0 + int(cell_h * 0.05)
    draw = ImageDraw.Draw(page)
    for dx, dy in ((-3, 0), (3, 0), (0, -3), (0, 3), (-2, -2), (2, 2), (-2, 2), (2, -2)):
        draw.text((tx + dx, ty + dy), text, font=font, fill=(255, 255, 255))
    draw.text((tx, ty), text, font=font, fill=ink)


def _cover_paste(page: Image.Image, img: Image.Image, box: tuple[int, int, int, int],
                 grayscale: bool = False) -> None:
    """把分镜图等比裁切填满格位。"""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    if grayscale:
        img = img.convert("L").convert("RGB")
    src_ratio = img.width / img.height
    dst_ratio = w / h
    if src_ratio > dst_ratio:
        new_h = h
        new_w = int(h * src_ratio)
    else:
        new_w = w
        new_h = int(w / src_ratio)
    resized = img.resize((max(1, new_w), max(1, new_h)), Image.LANCZOS)
    left = (new_w - w) // 2
    top = (new_h - h) // 2
    cropped = resized.crop((left, top, left + w, top + h))
    page.paste(cropped, (x0, y0))


def _resolve_panel_image(panel: dict) -> Optional[Path]:
    """定位分镜图的本地路径（优先用图片记录的绝对路径）。"""
    image_id = panel.get("image_id")
    if image_id:
        try:
            from app import repo

            record = repo.get_image(image_id)
            if record and record.get("path"):
                p = resolve_stored(record["path"])
                if p and p.exists():
                    return p
        except Exception:
            pass
    url = panel.get("image_url") or ""
    if not url:
        return None
    p = Path(url)
    if not p.is_absolute():
        # 静态 URL 形如 /data/outputs/... → 相对项目根解析，避免受进程工作目录影响
        p = settings.project_root / url.lstrip("/\\")
    return p if p.exists() else None


def render_project_pages(
    project_id: str,
    layout: str | None = None,
    page_width: int = 1240,
    gutter: int = 16,
    margin: int = 24,
    bubble_position: str = "auto",
    grayscale: bool | None = None,
    show_page_number: bool = True,
) -> dict:
    """把已生成的分镜排版成漫画页，返回生成的成品列表。

    P4 参数重定：气泡字号、边框线宽、页脚字号都改成**按页宽比例**算
    （原来是按 768 页宽调的固定系数），这样页宽从 1240 拉到 1800 也不会失调。
    主题色若已设置，气泡描边 / 页码 / 分格边框都会跟着走，整本立刻成套。
    """
    project = crepo.get_project(project_id)
    if not project:
        raise ComicError("项目不存在")

    panels = [p for p in crepo.list_panels(project_id) if p.get("image_url")]
    if not panels:
        raise ComicError("还没有已生成的分镜图，请先出图")

    layout_key = layout or project.get("layout") or "grid_2x2"
    lay = LAYOUT_PRESETS.get(layout_key, LAYOUT_PRESETS["grid_2x2"])
    cols, rows = int(lay["cols"]), int(lay["rows"])
    per_page = cols * rows

    # 排版一律保留分镜图原有色彩（黑白与否由导出时的「黑白版/彩色版」选项决定，
    # 灰度烘焙进成品页后彩色就再也找不回来了）
    grayscale = bool(grayscale)

    # 主题色（主色 + 点缀色）：跟随项目设置，未设时是近黑 + 中灰
    ink, accent = _theme_rgb(project)

    page_width = max(600, min(int(page_width), 2400))
    # 页宽比例（相对 1240 的基准）——所有线宽 / 字号 / 间距按它缩放
    scale = page_width / 1240.0
    margin = max(12, int(margin * scale))
    gutter = max(6, int(gutter * scale))
    inner_w = page_width - margin * 2
    cell_w = int((inner_w - gutter * (cols - 1)) / cols)
    aspect = float(project.get("height") or 768) / float(project.get("width") or 768)
    cell_h = int(cell_w * max(0.5, min(2.0, aspect)))

    body_h = rows * cell_h + gutter * (rows - 1)
    footer = max(24, int(34 * scale)) if show_page_number else 0
    page_h = margin * 2 + body_h + footer

    # 成品页与分镜图/角色图/场景图放在同一个项目目录里
    out_dir = project_output_dir(project)

    produced: list[dict] = []
    total_pages = (len(panels) + per_page - 1) // per_page

    for page_idx in range(total_pages):
        chunk = panels[page_idx * per_page:(page_idx + 1) * per_page]
        page = Image.new("RGB", (page_width, page_h), (255, 255, 255))
        draw = ImageDraw.Draw(page)

        for slot, panel in enumerate(chunk):
            r, c = divmod(slot, cols)
            x0 = margin + c * (cell_w + gutter)
            y0 = margin + r * (cell_h + gutter)
            box = (x0, y0, x0 + cell_w, y0 + cell_h)

            try:
                src_path = _resolve_panel_image(panel)
                if not src_path:
                    raise FileNotFoundError(panel.get("image_url") or "未生成")
                with Image.open(src_path) as src:
                    _cover_paste(page, src.convert("RGB"), box, grayscale=grayscale)
            except Exception:
                draw.rectangle(box, fill=(238, 239, 242), outline=(180, 184, 192))
                continue

            # 分格边框：跟随主题色（原来写死近黑）
            draw.rectangle(box, outline=ink, width=max(2, int(cell_w * 0.006)))
            if panel.get("dialogue"):
                _draw_bubble(page, box, panel["dialogue"], position=bubble_position,
                             index=slot, font_scale=scale, ink=ink)
            if panel.get("sfx"):
                _draw_sfx(page, box, panel["sfx"], ink=accent if accent != ink else ink)

        if show_page_number:
            f = _load_font(max(13, int(page_width * 0.014)))
            label = f"{project['title']}  ·  {page_idx + 1} / {total_pages}"
            w = f.getlength(label)
            draw.text(((page_width - w) / 2, page_h - margin - 16), label, font=f, fill=accent)

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        fname = f"{ts}_{project_id[-6:]}_p{page_idx + 1}.png"
        path = out_dir / fname
        page.save(path, format="PNG", optimize=True)

        produced.append(crepo.create_render(
            project_id=project_id,
            path=stored_path(path),
            url=relative_url(path),
            layout=layout_key,
            page=page_idx + 1,
            width=page_width,
            height=page_h,
        ))

    crepo.update_project(project_id, layout=layout_key)
    return {
        "pages": len(produced),
        "layout": layout_key,
        "layout_name": lay["name"],
        "items": produced,
    }


def project_exports(project: dict, limit: int = 20) -> list[dict]:
    """列出项目目录下已导出的 PDF（整部漫画合订本），按时间倒序。"""
    try:
        out_dir = project_output_dir(project)
    except Exception:  # pragma: no cover - 防御性
        return []
    items: list[dict] = []
    for p in out_dir.glob("*.pdf"):
        try:
            st = p.stat()
        except OSError:
            continue
        items.append({
            "name": p.name,
            "url": relative_url(p),
            "path": str(p),
            "size": st.st_size,
            "created_at": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        })
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items[:limit]


def delete_project_export(project_id: str, filename: str) -> None:
    """删除项目目录下的某个导出 PDF（只允许删自己目录里的 .pdf）。"""
    project = crepo.get_project(project_id)
    if not project:
        raise ComicError("项目不存在")
    name = Path(str(filename or "").replace("\\", "/")).name      # 只取文件名，杜绝路径穿越
    if not name or not name.lower().endswith(".pdf"):
        raise ComicError("只能删除 PDF 文件")
    target = project_output_dir(project) / name
    if not target.is_file():
        raise ComicError("文件不存在")
    try:
        target.unlink()
    except OSError as exc:
        raise ComicError(f"删除失败：{exc}")


def export_project_pdf(
    project_id: str,
    layout: str | None = None,
    page_width: int = 1240,
    bubble_position: str = "auto",
    grayscale: bool | None = None,
    show_page_number: bool = True,
    rerender: bool = False,
    color_mode: str = "color",
) -> dict:
    """把成品页合订成一份 PDF —— 整部漫画一次导出。

    - 直接读盘上已有的成品 PNG，按页码顺序合并，导出结果与界面所见完全一致；
    - 一页成品都没有时，先按当前设置自动排版一次（这样「一键导出」也能用）；
    - color_mode：`color`=彩色版（保留成品页原有色彩）；`bw`=黑白版（合成时转灰度，
      不破坏磁盘上的原文件）；
    - PDF 落在项目目录 `outputs/comics/<项目名>_<id尾号>/` 下，与出图放在一起。
    """
    bw = str(color_mode or "color").lower() in ("bw", "gray", "grayscale", "black")
    project = crepo.get_project(project_id)
    if not project:
        raise ComicError("项目不存在")

    if rerender or not crepo.list_renders(project_id, limit=1):
        render_project_pages(
            project_id,
            layout=layout,
            page_width=page_width,
            bubble_position=bubble_position,
            show_page_number=show_page_number,
        )

    # 每点一次「生成漫画页」都会新增一批成品页（旧批次保留在列表里可回看），
    # 所以同一个页码可能有多份历史版本 —— 导出时每个页码只取最新的一份，
    # 否则重排一次 PDF 里就会出现重复页。
    latest_by_page: dict[int, dict] = {}
    for r in crepo.list_renders(project_id, limit=1000):
        page_no = int(r.get("page") or 0)
        cur = latest_by_page.get(page_no)
        if cur is None or (r.get("created_at") or "") > (cur.get("created_at") or ""):
            latest_by_page[page_no] = r
    renders = [latest_by_page[k] for k in sorted(latest_by_page)]
    if not renders:
        raise ComicError("还没有成品页可导出，请先出图并生成漫画页")

    images: list[Image.Image] = []
    try:
        for r in renders:
            # path / url 双候选：跨机器拷库后 path 可能指向不存在的旧盘符，
            # 按候选顺序取第一个真实存在的文件
            path = _resolve_existing(r.get("path"), r.get("url"))
            if not path:
                continue
            with Image.open(path) as src:
                img = src.convert("RGB")
                if bw:
                    img = img.convert("L").convert("RGB")   # 黑白版：合成时转灰度
                images.append(img)
    except Exception as exc:
        for im in images:
            im.close()
        raise ComicError(f"读取成品页失败：{exc}")
    if not images:
        raise ComicError("成品页文件缺失，请重新「生成漫画页」")

    out_dir = project_output_dir(project)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    title = _safe_dir_name(project.get("title") or "") or "comic"
    pdf_path = out_dir / (f"{title}_黑白版_{ts}.pdf" if bw else f"{title}_{ts}.pdf")

    # 页宽 1240px 时 DPI≈150，正好是 A4 宽度（210mm），打印/阅读尺寸都合适
    first_w = max(1, images[0].width)
    dpi = max(72, min(300, round(first_w / (210 / 25.4))))
    try:
        images[0].save(
            pdf_path,
            format="PDF",
            save_all=True,
            append_images=images[1:],
            resolution=dpi,
        )
    except Exception as exc:
        raise ComicError(f"生成 PDF 失败：{exc}")
    finally:
        for im in images:
            im.close()

    return {
        "url": relative_url(pdf_path),
        "path": str(pdf_path),
        "filename": pdf_path.name,
        "pages": len(images),
        "size": pdf_path.stat().st_size,
    }


def page_size_for_layout(layout: str | None) -> int:
    """每个排版页容纳的分镜数。"""
    lay = LAYOUT_PRESETS.get(layout or "grid_2x2", LAYOUT_PRESETS["grid_2x2"])
    return int(lay["cols"]) * int(lay["rows"])


def presets() -> dict:
    """前端预设：画风库（分组 + 全量索引）、主题色、景别、排版、生成档位、节奏模板。

    画风库是全量 327 条但有分组，前端可分组筛选 / 搜索；这里一次性下发**轻量索引**
    （key / 分组 / 名称 / 参考作者 / 是否有缩略图），不含 traits 长文本，
    避免 bootstrap 就把 ~130KB 全塞给前端。
    """
    lib = style_library()
    styles_light = []
    for s in lib.list_styles():
        styles_light.append({
            "key": s.get("key"),
            "group": s.get("group") or "",
            "name": s.get("name") or "",
            "name_en": s.get("name_en") or "",
            "reference": s.get("reference") or "",
            "legacy": bool(s.get("legacy")),
            "thumb": bool(lib.reference_image(s.get("key"))),
        })
    colors = []
    for c in lib.colors:
        colors.append({
            "key": c.get("id"),
            "name": c.get("name_zh") or "",
            "name_en": c.get("name_en") or "",
            "category": c.get("category_zh") or "",
            "quote": c.get("quote_zh") or "",
            "hex": lib.color_hex(c.get("id")),
        })
    rhythms = []
    for x in lib.sb_templates():
        info = rhythm.build(x.get("id")) or {}
        rhythms.append({
            "key": x.get("id"),
            "name": x.get("name") or "",
            "name_en": x.get("name_en") or "",
            "keywords": x.get("keywords") or [],
            "family": info.get("family") or "",
            "family_name": info.get("family_name") or "其他",
            "degraded": bool(info.get("degraded")),
        })
    return {
        "styles": styles_light,
        "style_groups": lib.groups(),
        "colors": colors,
        "profiles": list_profiles(),
        "default_profile": DEFAULT_PROFILE,
        "rhythms": rhythms,
        "rhythm_families": rhythm.list_families(),
        "shots": [{"key": k, **v} for k, v in SHOT_PRESETS.items()],
        "layouts": [{"key": k, **v} for k, v in LAYOUT_PRESETS.items()],
        "bubble_positions": [
            {"key": "auto", "name": "自动交替"},
            {"key": "top", "name": "统一顶部"},
            {"key": "bottom", "name": "统一底部"},
        ],
        "attribution": style_attribution(),
    }


def style_detail(key: str) -> dict:
    """单个画风的完整口径（含 traits 长文本），供前端"详情卡"按需拉取。"""
    lib = style_library()
    info = lib.expand(key)
    return {
        **info,
        "thumb": lib.thumb_url(key),
    }


def panel_to_dict(panel: dict) -> dict:
    """把 character_ids 反序列化，便于前端使用。"""
    out = dict(panel)
    try:
        out["character_ids"] = json.loads(panel.get("character_ids") or "[]")
    except Exception:
        out["character_ids"] = []
    return out


def panel_generation_info(panel_id: str) -> dict:
    """分镜出图的完整口径：提示词、参考图构成、采样参数。

    给前端「词」按钮用，方便核对一致性参数到底生效了没有。
    """
    panel = crepo.get_panel(panel_id)
    if not panel:
        raise ComicError("分镜不存在")
    project = crepo.get_project(panel["project_id"]) or {}
    characters = crepo.list_characters(panel["project_id"])
    ref = decide_panel_refs(project, panel, characters, load=False)
    prompt, negative = build_prompt(project, panel, characters, ref_slots=ref["slots"])
    selected = _characters_for(panel, characters)
    refs = [c for c in selected
            if (rp := resolve_stored(c.get("ref_path"))) and rp.exists()]
    ref_names = {c["id"] for c in refs}
    lib = style_library()
    return {
        "prompt": prompt,
        "negative_prompt": negative,
        "use_img2img": bool(ref["refs"] or ref["slots"]),
        "ref_kind": ref["ref_kind"],
        "ref_slots": ref["slots"],
        "ref_characters": [s["name"] for s in ref["slots"] if s["kind"] == "portrait"],
        "missing_ref": [c["name"] for c in selected if c["id"] not in ref_names],
        "ref_composite": False,          # 不再拼贴：多图作为独立参考图喂给 2.1 编辑通道
        "fusion": ref["mode"] == "fusion",
        "strength": 1.0,
        "cfg": 1.0 if ref["mode"] == "fusion" else float(project.get("guidance") or 4.0),
        "seed": int(panel.get("seed") or -1),
        "shot": panel.get("shot"),
        "style": project.get("style"),
        # 画风完整口径（供「词」面板展示到底注入了什么）
        "style_info": {
            "key": lib.style(project.get("style")).get("key"),
            "name": lib.style(project.get("style")).get("name"),
            "group": lib.style(project.get("style")).get("group"),
            "reference": lib.style(project.get("style")).get("reference"),
            "legacy": lib.style(project.get("style")).get("legacy"),
        },
        "theme_color": project.get("theme_color") or "",
        "theme_color2": project.get("theme_color2") or "",
        "theme_line": theme_prompt_text(project),
        "profile": project.get("profile") or "",
        "batch": int(project.get("batch") or 1),
        "negative_is_default": not (project.get("negative") or "").strip(),
        "char_negative_is_default": not (project.get("char_negative") or "").strip(),
    }


def project_detail(project_id: str) -> dict:
    project = crepo.get_project(project_id)
    if not project:
        raise ComicError("项目不存在")
    try:
        reconcile_project(project_id)
    except Exception:
        pass
    return {
        "project": project,
        "characters": crepo.list_characters(project_id),
        "scenes": crepo.list_scenes(project_id),
        "panels": [panel_to_dict(p) for p in crepo.list_panels(project_id)],
        "renders": crepo.list_renders(project_id, limit=12),
        "exports": project_exports(project),
        "stats": crepo.project_stats(project_id),
        "scene_stats": _scene_stats(project_id),
    }


def _scene_stats(project_id: str) -> dict:
    scenes = crepo.list_scenes(project_id)
    return {
        "total": len(scenes),
        "done": sum(1 for s in scenes if s["status"] == "done"),
        "active": sum(1 for s in scenes if s["status"] in ("queued", "running")),
        "failed": sum(1 for s in scenes if s["status"] == "failed"),
    }


def create_panels_from_script(project_id: str, text: str, replace: bool = True) -> dict:
    """解析剧本并写入分镜。

    若项目选了分镜节奏模板（handraw SB-xxx），则按模板的镜头序列重排景别
    （**逐格通路**下的轻量用法：只影响这一页几格与镜头序列，不改出图方式）。
    """
    parsed = parse_script(text)
    if not parsed:
        raise ComicError("没有解析出任何分镜，请检查剧本内容")

    project = crepo.get_project(project_id) or {}

    # 节奏模板：只覆盖"自动分配"的景别，用户显式标注的 [特写] 不覆盖
    rhythm_shots = rhythm.shot_cycle(project.get("rhythm_template"))
    if rhythm_shots:
        auto_i = 0
        for item in parsed:
            if item.get("shot_explicit"):
                continue                     # 尊重剧本里显式写的景别
            item["shot"] = rhythm_shots[auto_i % len(rhythm_shots)]
            auto_i += 1

    if replace:
        for p in crepo.list_panels(project_id):
            crepo.delete_panel(p["id"])
        start_seq = 1
    else:
        start_seq = crepo.next_seq(project_id)

    lay = LAYOUT_PRESETS.get(project.get("layout") or "grid_2x2", LAYOUT_PRESETS["grid_2x2"])
    per_page = int(lay["cols"]) * int(lay["rows"])

    for i, item in enumerate(parsed):
        seq = start_seq + i
        crepo.create_panel(
            project_id,
            seq=seq,
            page=item["page"],
            cell=(seq - 1) % per_page,
            shot=item["shot"],
            scene=item["scene"],
            dialogue=item["dialogue"],
            sfx=item["sfx"],
            character_ids=[],
            status="draft",
        )
    crepo.update_project(project_id, synopsis=text)
    crepo.renumber(project_id, page_size=per_page)
    return {"created": len(parsed), "items": [panel_to_dict(p) for p in crepo.list_panels(project_id)]}


def save_character_ref(character_id: str, data_url: str) -> dict:
    """保存角色参考图到 data/refs/。"""
    from app.models_util import decode_base64_image

    try:
        img = decode_base64_image(data_url)
    except Exception as exc:
        raise ComicError(f"参考图解析失败：{exc}") from exc

    ref_dir = settings.data_dir_path / "refs"
    ref_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{character_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.png"
    path = ref_dir / fname
    img.save(path, format="PNG", optimize=True)
    crepo.update_character(character_id, ref_path=stored_path(path), ref_url=relative_url(path),
                           ref_source="upload", status="done", error=None)
    return {"path": stored_path(path), "url": relative_url(path)}


def new_project_from_payload(data: dict) -> dict:
    title = (data.get("title") or "").strip() or f"未命名漫画 {now_iso()[5:16]}"
    # 画风 / 版式：兼容旧 key（jp_bw…）与新编号（FA-001…）；非法值回落到默认
    style_key = str(data.get("style") or "").strip()
    style = style_key or "jp_bw"
    layout = data.get("layout") if data.get("layout") in LAYOUT_PRESETS else "grid_2x2"
    try:
        ref_strength = float(data.get("ref_strength") or 0.55)
    except Exception:
        ref_strength = 0.55

    # 生成档位（dev / target）：显式给了尺寸/步数就用显式值，否则用档位默认值。
    # 不传档位时退回 .env 的 DEFAULT_*，保持与旧行为一致。
    profile_key = str(data.get("profile") or "").strip().lower()
    prof = get_profile(profile_key)
    width = int(data.get("width") or (prof["width"] if prof else settings.default_width))
    height = int(data.get("height") or (prof["height"] if prof else settings.default_height))
    steps = int(data.get("steps") or (prof["steps"] if prof else settings.default_steps))
    guidance = float(data.get("guidance") or (prof["guidance"] if prof else settings.default_guidance))
    batch = int(data.get("batch") or (prof["batch"] if prof else 1))

    return crepo.create_project(
        title=title,
        style=style,
        layout=layout,
        width=width,
        height=height,
        steps=steps,
        guidance=guidance,
        # 默认就把反向提示词填好，压制畸形 / 多指 / 怪表情
        negative=(data.get("negative") or "").strip() or DEFAULT_PROJECT_NEGATIVE,
        char_negative=(data.get("char_negative") or "").strip() or DEFAULT_CHAR_NEGATIVE,
        ref_strength=max(0.2, min(0.95, ref_strength)),
        synopsis=(data.get("synopsis") or "").strip(),
        keep_style=bool(data.get("keep_style", True)),
        use_ref=bool(data.get("use_ref", True)),
        profile=profile_key if prof else "",
        theme_color=str(data.get("theme_color") or "").strip(),
        theme_color2=str(data.get("theme_color2") or "").strip(),
        rhythm_template=str(data.get("rhythm_template") or "").strip(),
        batch=max(1, min(4, batch)),
    )
