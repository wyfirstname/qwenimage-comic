# -*- coding: utf-8 -*-
"""拼豆内置色板。

色值说明（诚实声明）：
    这里每一条 hex 都是按市面上常见的「漫豆 / Mard 2.6mm 实色豆」视觉近似取值，
    不是某品牌官方色号表——官方色卡不公开且各批有色差。实拼时以实际豆子微调即可。
    需要对接自己的色卡时，直接改 data/perler/palettes.json（格式见 DEFAULT_PALETTES），
    后端启动时若存在该文件会覆盖内置色板，不用改代码。
"""

from __future__ import annotations

import json
from pathlib import Path

from shared.config import settings

# 每条颜色：(id, 中文名, hex)
DEFAULT_PALETTES: dict[str, dict] = {
    "basic24": {
        "name": "基础 24 色",
        "colors": [
            ["white",    "白色",   "#FFFFFF"],
            ["cream",    "米白",   "#F5EFD8"],
            ["skin",     "肤色",   "#F7CDA9"],
            ["pink",     "粉色",   "#F6A8C0"],
            ["rose",     "玫红",   "#E85A7B"],
            ["red",      "大红",   "#D93A35"],
            ["wine",     "酒红",   "#8E2A3C"],
            ["orange",   "橙色",   "#F08300"],
            ["amber",    "杏黄",   "#F5B041"],
            ["yellow",   "中黄",   "#F7D31E"],
            ["lemon",    "柠檬黄", "#F9EE70"],
            ["grass",    "草绿",   "#7CB342"],
            ["green",    "深绿",   "#2E7D32"],
            ["olive",    "橄榄绿", "#6B7A3A"],
            ["mint",     "薄荷绿", "#9FE2BF"],
            ["cyan",     "湖蓝",   "#2BB3C0"],
            ["sky",      "天蓝",   "#5AA7E8"],
            ["blue",     "深蓝",   "#2851A3"],
            ["navy",     "藏青",   "#1F2E5C"],
            ["purple",   "紫色",   "#7E57C2"],
            ["lavender", "浅紫",   "#C5A8E8"],
            ["brown",    "棕色",   "#8D5A2B"],
            ["darkbrown", "深棕",  "#4E342E"],
            ["black",    "黑色",   "#202020"],
        ],
    },
    "common48": {
        "name": "常用 48 色",
        "colors": [
            ["white",    "白色",   "#FFFFFF"],
            ["cream",    "米白",   "#F5EFD8"],
            ["ivory",    "象牙",   "#EDE4C8"],
            ["silver",   "浅灰",   "#C9CDD3"],
            ["grey",     "中灰",   "#8C9199"],
            ["slate",    "深灰",   "#565B63"],
            ["black",    "黑色",   "#202020"],
            ["skin",     "肤色",   "#F7CDA9"],
            ["blush",    "腮红",   "#F2B3A0"],
            ["tan",      "小麦",   "#D9A066"],
            ["choc",     "巧克力", "#6B4226"],
            ["brown",    "棕色",   "#8D5A2B"],
            ["darkbrown", "深棕",  "#4E342E"],
            ["pink",     "粉色",   "#F6A8C0"],
            ["hotpink",  "桃粉",   "#F27BA5"],
            ["rose",     "玫红",   "#E85A7B"],
            ["magenta",  "桃红",   "#D63384"],
            ["red",      "大红",   "#D93A35"],
            ["darkred",  "深红",   "#A82A28"],
            ["wine",     "酒红",   "#8E2A3C"],
            ["coral",    "珊瑚橙", "#F5795B"],
            ["orange",   "橙色",   "#F08300"],
            ["amber",    "杏黄",   "#F5B041"],
            ["yellow",   "中黄",   "#F7D31E"],
            ["lemon",    "柠檬黄", "#F9EE70"],
            ["khaki",    "卡其",   "#C3B091"],
            ["grass",    "草绿",   "#7CB342"],
            ["apple",    "苹果绿", "#8BC34A"],
            ["mint",     "薄荷绿", "#9FE2BF"],
            ["olive",    "橄榄绿", "#6B7A3A"],
            ["green",    "深绿",   "#2E7D32"],
            ["pine",     "松绿",   "#1B5E4A"],
            ["teal",     "青绿",   "#1E8E8E"],
            ["cyan",     "湖蓝",   "#2BB3C0"],
            ["sky",      "天蓝",   "#5AA7E8"],
            ["azure",    "湛蓝",   "#3B7BD8"],
            ["blue",     "深蓝",   "#2851A3"],
            ["navy",     "藏青",   "#1F2E5C"],
            ["lavender", "浅紫",   "#C5A8E8"],
            ["purple",   "紫色",   "#7E57C2"],
            ["violet",   "深紫",   "#5E35B1"],
            ["lilac",    "丁香",   "#B39DDB"],
            ["mauve",    "藕荷",   "#9A7AA0"],
            ["peach",    "蜜桃",   "#FFCBA4"],
            ["apricot",  "杏色",   "#FAD0A0"],
            ["sand",     "沙色",   "#D8C3A5"],
            ["coffee",   "咖啡",   "#7A5230"],
            ["walnut",   "胡桃",   "#5D4037"],
        ],
    },
}


def _external_file() -> Path:
    return settings.data_dir_path / "perler" / "palettes.json"


def load_palettes() -> dict[str, dict]:
    """内置色板 + 可选的用户覆盖文件（data/perler/palettes.json）。"""
    palettes = {k: {**v, "id": k} for k, v in DEFAULT_PALETTES.items()}
    ext = _external_file()
    if ext.exists():
        try:
            data = json.loads(ext.read_text(encoding="utf-8"))
            for pid, p in data.items():
                if isinstance(p, dict) and p.get("colors"):
                    palettes[pid] = {**p, "id": pid}
        except Exception as exc:  # pragma: no cover - 防御性
            print(f"[perler] 读取用户色板失败（用内置色板兜底）：{exc}")
    return palettes


def get_palette(palette_id: str) -> dict:
    palettes = load_palettes()
    if palette_id not in palettes:
        raise KeyError(f"未知色板：{palette_id}（可选：{', '.join(palettes)}）")
    return palettes[palette_id]
