# -*- coding: utf-8 -*-
"""画风库 / 主题色 / 分镜节奏模板（接入 yang0/handraw-style，MIT）。

数据来源：https://github.com/yang0/handraw-style （v1.8.5）
- styles.json            327 条手绘风格（8 组 FA-FH）
- colors.json            36 条主题色
- layouts.json           165 条排版图型（其中 SB-* 漫画分镜 71 条）
- style_alias_map.json   旧编号 001-327 ↔ 新编号 FA-xxx
- model_capabilities.json 按模型标定 name_activation / traits_activation

⚠️ 授权合规：MIT 但作者要求保留 yang0 与原仓库地址，
   所以任何展示画风库的界面都要署名（见 attribution()）。

🔴 必须屏蔽的一块：handraw 的 `graphic-text` 图文模式（文字参与构图）。
   本项目的漫画策略是「文字一律 PIL 画、提示词里明确禁字」，
   两者直接冲突 —— 只借「画风 / 主题色 / 分镜节奏」三块，
   `graphic-text` 后缀一律不注入。见 `forbidden_suffixes`。

设计要点：
  * 全部数据是**只读**的第三方快照，加载为内存单例（进程内只读一次）；
  * `resolve()` 兼容四种写法：`jp_bw`（本项目旧 key）/ `FA-001` / `001` / 中文名；
  * `expand()` 产出「要注入提示词的文本 + 是否挂参考图」，供 comic.py 拼装用；
  * 缩略图**不进 git**（体积），由 scripts/fetch_style_library.py 抓取到 thumbs/，
    绿色包构建时一并打进包；缺失时界面自动降级为纯文字网格。
"""

from __future__ import annotations

import json
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

DATA_DIR = Path(__file__).resolve().parent / "data"
THUMBS_DIR = Path(__file__).resolve().parent / "thumbs"

# 第三方快照版本（用于将来 diff 增量升级）
_VERSION_FILE = DATA_DIR / "handraw_version.json"

# handraw 的图文模式后缀：文字参与构图，与本项目「文字一律 PIL 画」正面冲突，必须屏蔽
FORBIDDEN_SUFFIXES = ("graphic-text", "graphic_text", "图文模式")

# ------------------------------------------------------------
#  本项目原有 5 个画风：在新库里保留为「常用」别名条目，存量项目零迁移
# ------------------------------------------------------------
LEGACY_STYLES: dict[str, dict[str, Any]] = {
    "jp_bw": {
        "key": "jp_bw",
        "group": "常用",
        "name": "日式黑白漫画",
        "name_en": "Japanese Manga (B&W)",
        "reference": "",
        "en": ("japanese manga style, monochrome black and white, clean ink line art, "
               "screentone shading, strong contrast, detailed linework"),
        "traits": "墨线 + 网点，杂志连载质感",
        "grayscale": True,
        "legacy": True,
    },
    "jp_color": {
        "key": "jp_color",
        "group": "常用",
        "name": "日式彩色动漫",
        "name_en": "Japanese Anime (Color)",
        "reference": "",
        "en": ("japanese anime style, cel shading, vibrant colors, clean line art, "
               "detailed background, soft lighting"),
        "traits": "赛璐璐上色，明亮通透",
        "grayscale": False,
        "legacy": True,
    },
    "american": {
        "key": "american",
        "group": "常用",
        "name": "美式漫画",
        "name_en": "American Comic",
        "reference": "",
        "en": ("american comic book style, bold ink outlines, dramatic chiaroscuro lighting, "
               "halftone dots, dynamic composition"),
        "traits": "粗描边 + 影调 + 半调网点",
        "grayscale": False,
        "legacy": True,
    },
    "ink": {
        "key": "ink",
        "group": "常用",
        "name": "国风水墨",
        "name_en": "Chinese Ink Wash",
        "reference": "",
        "en": ("chinese ink wash painting style, sumi-e brush strokes, elegant negative space, "
               "muted traditional palette, xuan paper texture"),
        "traits": "水墨写意，留白构图",
        "grayscale": False,
        "legacy": True,
    },
    "webtoon": {
        "key": "webtoon",
        "group": "常用",
        "name": "韩式条漫",
        "name_en": "Korean Webtoon",
        "reference": "",
        "en": ("korean webtoon style, soft cel shading, pastel palette, clean digital line art, "
               "polished background"),
        "traits": "柔和上色，竖屏阅读",
        "grayscale": False,
        "legacy": True,
    },
}


class StyleLibrary:
    """只读单例：加载第三方快照并提供查询 / 展开能力。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loaded = False
        self.styles: list[dict] = []
        self.colors: list[dict] = []
        self.layouts: list[dict] = []
        self.alias: dict[str, str] = {}
        self.model_caps: dict = {}
        self.colors_hex: dict[str, str] = {}
        self.version: str = "unknown"
        self._by_key: dict[str, dict] = {}
        self._styles_en: dict[str, str] = {}   # 自动生成的英文 trait 摘要（可选）

    # ---------------- 加载 ----------------
    def load(self) -> "StyleLibrary":
        if self._loaded:
            return self
        with self._lock:
            if self._loaded:
                return self
            self.styles = self._read_json("styles.json", [])
            self.colors = self._read_json("colors.json", [])
            self.layouts = self._read_json("layouts.json", [])
            alias = self._read_json("style_alias_map.json", {})
            self.alias = (alias.get("legacy_to_new") or {}) if isinstance(alias, dict) else {}
            self.model_caps = self._read_json("model_capabilities.json", {})
            # 本项目补的 hex（上游 colors.json 只有名称，排版配色需要具体色值）
            self.colors_hex = self._read_json("colors_hex.json", {}) or {}
            ver = self._read_json("handraw_version.json", {})
            self.version = (ver or {}).get("version") or "unknown"
            # 可选的英文 traits 摘要（机器译 + 可人工修）；缺失时留空
            self._styles_en = self._read_json("traits_en.json", {}) or {}

            # 建索引：新编号为主键，兼容旧编号与中文名
            self._by_key = {k: dict(v) for k, v in LEGACY_STYLES.items()}
            for s in self.styles:
                num = str(s.get("number") or "").strip()
                if num:
                    self._by_key[num] = self._normalize_style(s)
            self._loaded = True
        return self

    @staticmethod
    def _read_json(name: str, default: Any) -> Any:
        p = DATA_DIR / name
        if not p.exists():
            return default
        try:
            with p.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:  # pragma: no cover - 防御性
            print(f"[style_library] 读取 {name} 失败：{exc}")
            return default

    def _normalize_style(self, s: dict) -> dict:
        """把 handraw 的原始条目规整成本项目内部结构（字段名逐字保留原义）。"""
        num = str(s.get("number") or "").strip()
        group = str(s.get("group") or "").strip()
        return {
            "key": num,
            "group": group,
            "group_code": group.split(" ")[0] if group else "",
            "reference": str(s.get("reference") or "").strip(),
            "name": str(s.get("generation_name") or "").strip(),
            "name_en": str(s.get("generation_name") or "").strip(),
            "traits": str(s.get("traits") or "").strip(),
            "traits_en": str(self._styles_en.get(num) or "").strip(),
            "grayscale": False,
            "legacy": False,
        }

    # ---------------- 查询 ----------------
    def style(self, key: str | None) -> dict:
        """按任意写法取一个画风条目；找不到时回落到第一个常用风格。

        兼容：`jp_bw` / `FA-001` / `001` / `FA 001` / 中文名。
        """
        self.load()
        if not key:
            return self._by_key.get("jp_bw") or next(iter(self._by_key.values()), {})
        k = str(key).strip()
        # 旧编号 001 → FA-001
        if k in self.alias and self.alias[k] in self._by_key:
            return self._by_key[self.alias[k]]
        if k in self._by_key:
            return self._by_key[k]
        # 大小写不敏感 + 去掉空格
        norm = k.upper().replace(" ", "")
        for cand, v in self._by_key.items():
            if str(cand).upper().replace(" ", "") == norm:
                return v
        # 中文名匹配
        for v in self._by_key.values():
            if v.get("name") == k or v.get("name_en") == k:
                return v
        return self._by_key.get("jp_bw") or next(iter(self._by_key.values()), {})

    def groups(self) -> list[dict]:
        """风格分组（含每组条数），供前端做分组筛选。"""
        self.load()
        seen: dict[str, int] = {}
        for s in self._by_key.values():
            g = s.get("group") or "未分组"
            seen[g] = seen.get(g, 0) + 1
        # 让「常用」永远排第一
        items = [{"group": g, "count": c} for g, c in seen.items()]
        items.sort(key=lambda x: (0 if x["group"] == "常用" else 1, x["group"]))
        return items

    def list_styles(self) -> list[dict]:
        self.load()
        order = {"常用": 0}
        return sorted(
            self._by_key.values(),
            key=lambda s: (order.get(s.get("group") or "", 1), s.get("group") or "", s.get("key") or ""),
        )

    def color(self, key: str | None) -> Optional[dict]:
        self.load()
        if not key:
            return None
        for c in self.colors:
            if c.get("id") == key or c.get("name_zh") == key or c.get("name_en") == key:
                out = dict(c)
                out["hex"] = self.colors_hex.get(str(c.get("id"))) or ""
                return out
        return None

    def color_hex(self, key: str | None) -> str:
        """主题色 → #RRGGBB；找不到返回空串（排版配色时回落到默认黑白）。"""
        c = self.color(key)
        return (c or {}).get("hex") or ""

    def color_prompt(self, key: str | None, lang: str = "zh") -> str:
        """主题色的提示词注入语句（上游就一句 `主题色：克莱因蓝（Klein Blue）。`）。"""
        c = self.color(key)
        if not c:
            return ""
        return (c.get("prompt_zh") if lang == "zh" else c.get("prompt_en")) or ""

    def layouts_by_category(self, category: str) -> list[dict]:
        self.load()
        return [x for x in self.layouts if x.get("category") == category]

    def sb_templates(self) -> list[dict]:
        """漫画分镜（SB-*）节奏模板清单。"""
        self.load()
        return [x for x in self.layouts if str(x.get("id", "")).startswith("SB-")]

    # ---------------- 展开 ----------------
    def expand(self, key: str | None, light: bool = False) -> dict:
        """产出一个画风的「提示词注入口径」。

        light=True（bootstrap 轻量装填用）时不读缩略图，只看文本。
        返回：
          key / group / name / reference / traits / traits_en
          prompt_text  —— 拼进正向提示词的文本（已按 handraw 展开格式组装）
          image        —— 该编号的参考图本地路径（缺失为 None），用于垫图兜底
          grayscale / legacy
        """
        st = self.style(key)
        img = self.reference_image(st.get("key"))
        traits = st.get("traits") or ""
        traits_en = st.get("traits_en") or ""
        name = st.get("name") or ""
        ref = st.get("reference") or ""

        if st.get("legacy"):
            text = st.get("en") or ""
        else:
            # handraw 展开格式：#编号 生成名。参考作者/风格名称：X。核心风格特征：Y。
            # 模型是 Qwen-Image（原生双语），中文 traits 可直接用；
            # 若补了英文摘要则中英并置，提高英文提示词路线的命中率。
            bits = [f"#{st.get('key')} {name}"]
            if ref:
                bits.append(f"reference artist / style: {ref}")
            if traits_en:
                bits.append(f"core style features: {traits_en}")
            if traits:
                bits.append(f"核心风格特征：{traits}")
            text = ". ".join(b for b in bits if b)

        return {
            "key": st.get("key"),
            "group": st.get("group") or "",
            "name": name,
            "name_en": st.get("name_en") or name,
            "reference": ref,
            "traits": traits,
            "traits_en": traits_en,
            "prompt_text": text,
            "image": img,
            "grayscale": bool(st.get("grayscale")),
            "legacy": bool(st.get("legacy")),
        }

    def reference_image(self, key: str | None) -> Optional[str]:
        """该编号的参考缩略图本地路径；不存在返回 None（界面自动降级）。"""
        if not key:
            return None
        for ext in (".webp", ".jpg", ".png"):
            p = THUMBS_DIR / f"{key}{ext}"
            if p.exists():
                return str(p)
        return None

    def thumb_url(self, key: str | None) -> Optional[str]:
        """缩略图的静态 URL（存在才给，供前端 <img> 用）。

        走独立的 `/style-thumbs` 挂载点（见 main.py），
        不用 /static 是为了让缩略图目录可以不存在（没抓取时不影响静态站挂载）。
        """
        if not key:
            return None
        for ext in (".webp", ".jpg", ".png"):
            if (THUMBS_DIR / f"{key}{ext}").exists():
                return f"/style-thumbs/{key}{ext}"
        return None


@lru_cache
def library() -> StyleLibrary:
    return StyleLibrary().load()


# ---------------- 便捷函数（供 comic.py / api 调用）----------------


def expand_style(key: str | None, light: bool = False) -> dict:
    return library().expand(key, light=light)


def attribution() -> dict:
    """署名信息（UI 页脚 + 关于页要用）。"""
    lib = library()
    return {
        "source": "yang0/handraw-style",
        "url": "https://github.com/yang0/handraw-style",
        "version": lib.version,
        "license": "MIT",
        "styles": len(lib.styles),
        "colors": len(lib.colors),
        "layouts": len(lib.layouts),
        "note": "画风库 / 主题色 / 分镜节奏数据来自 yang0/handraw-style（MIT），"
                "按作者要求保留原仓库署名。",
    }
