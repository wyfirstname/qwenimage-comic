# -*- coding: utf-8 -*-
"""生成档位（Generation Profile）—— 一套代码适配两种硬件。

背景：分辨率 / 步数 / CFG / batch / lowvram 这些值原先是**建项目时写死**
的 768 / 8 / 4.0 / 1。本机（RTX 2060 6GB）只做开发与功能验证，
用户会在另一台 16GB 显卡机器上测试画质，两套硬件共用一份代码就必须把它们
做成可切换的「档位」。

| 档位 | 分辨率 | 步数 | 说明 |
|---|---|---|---|
| dev      | 768×768   | 8  | 开发档：本机跑得动，只验功能正确性 |
| target   | 1024×1024 | 24 | 目标档：16GB 用，画质与风格还原的判据 |
| custom   | 用户自定  | 用户自定 | 手动微调 |

🔴 铁律：本机（6GB）的出图结果**不能**作为画质 / 风格还原的判据，
   只能判断「结构没崩、拼装没问题」。画风像不像一律以 16GB 机实测为准。
"""

from __future__ import annotations

from typing import Any, Optional

# 档位定义。字段含义：
#   width/height 单格出图尺寸
#   steps        采样步数
#   guidance     引导系数（融合路径会被强制 CFG=1，此处对 txt2img 生效）
#   batch        一次出几张候选（1=不做候选）
#   low_vram     是否走 --lowvram（6GB 建议 true；16GB 建议 false）
#   text_encoder_device 文本编码器设备（6GB 必须 cpu；16GB 可 cuda）
#   keep_loaded  是否常驻显存
PROFILES: dict[str, dict[str, Any]] = {
    "dev": {
        "key": "dev",
        "name": "开发档 · 6GB 友好",
        "desc": "768×768 / 8 步 / lowvram；跑得通、接口对、不崩，不用于判断画质",
        "width": 768,
        "height": 768,
        "steps": 8,
        "guidance": 4.0,
        "batch": 1,
        "low_vram": True,
        "text_encoder_device": "cpu",
        "keep_loaded": True,
    },
    "target": {
        "key": "target",
        "name": "目标档 · 16GB 画质",
        "desc": "1024×1024 / 24 步 / 正常 CFG；16GB 上画质与风格还原的唯一判据",
        "width": 1024,
        "height": 1024,
        "steps": 24,
        "guidance": 4.0,
        "batch": 1,
        "low_vram": False,
        "text_encoder_device": "cuda:0",
        "keep_loaded": True,
    },
}

DEFAULT_PROFILE = "dev"


def get_profile(name: str | None) -> Optional[dict]:
    """取一个档位定义；未知名字返回 None（交给调用方用项目自带值）。"""
    if not name:
        return None
    return PROFILES.get(str(name).strip().lower())


def apply_to_project_fields(profile_key: str | None, base: dict | None = None) -> dict:
    """把档位默认值合并进一组项目字段（用户显式传入的值优先）。"""
    base = dict(base or {})
    prof = get_profile(profile_key)
    if not prof:
        return base
    for k in ("width", "height", "steps", "guidance"):
        if base.get(k) in (None, "", 0):
            base[k] = prof[k]
    return base


def list_profiles() -> list[dict]:
    return list(PROFILES.values())


def infer_profile(width: int | None, height: int | None, steps: int | None) -> str:
    """按尺寸 / 步数反推最接近的档位（用于存量项目展示）。"""
    try:
        w, h, s = int(width or 0), int(height or 0), int(steps or 0)
    except Exception:
        return "custom"
    for key, prof in PROFILES.items():
        if w == prof["width"] and h == prof["height"] and s == prof["steps"]:
            return key
    return "custom"
