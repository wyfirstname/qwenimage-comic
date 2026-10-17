"""提交前用 ComfyUI 自己的 /object_info 校对输入键。

为什么需要这层
--------------
部分节点（如 TextEncodeQwenImage21 的 images）是 Autogrow 动态输入。在 API 格式里，
每个槽位的真名是「父名.子名」——images.image_1、images.image_2……。
写成嵌套字典 ``images={"image_1": [...]}`` 时，展开后的 schema 里根本没有 ``images``
这个键，ComfyUI 会把整个输入**静默丢弃**：节点收到 None，参考图一张都不进模型，
编辑退化成文生图。不报错、不告警、进度条照走，只是每格各画一个人。

这种错误 mock 引擎永远发现不了（mock 不做 schema 校验），只能靠向真实服务校对。
本模块在提交前拉一次 /object_info（按节点类型缓存），逐个核对：工作流里出现的键
是否真能到达节点。发现问题就抛错，把静默失败变成一句能照着改的报错。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from shared.config import settings

# 节点类型 → object_info（进程内缓存；ComfyUI 重启后才需要重新拉）
_INFO_CACHE: dict[str, dict] = {}

# 动态输入的标量类型前缀：COMFY_AUTOGROW_V3 等
_AUTOGROW_PREFIX = "COMFY_AUTOGROW"


class SchemaError(RuntimeError):
    """工作流与目标 ComfyUI 的节点契约不匹配，提交上去也会被静默丢弃。"""


def _object_info(class_type: str) -> dict | None:
    if class_type in _INFO_CACHE:
        return _INFO_CACHE[class_type]
    url = f"{settings.comfyui_url}/object_info/{class_type}"
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        _INFO_CACHE[class_type] = {}      # 连不上就记空，本次跳过校验
        return None
    info = data.get(class_type) or {}
    _INFO_CACHE[class_type] = info
    return info or None


def _autogrow_slots(info: dict) -> dict[str, list[str]]:
    """节点声明了哪些动态输入：{输入名: 允许的槽位名}。"""
    out: dict[str, list[str]] = {}
    for group in ("required", "optional"):
        for name, spec in (info.get("input", {}).get(group) or {}).items():
            if not isinstance(spec, (list, tuple)) or not spec:
                continue
            kind = spec[0]
            if not isinstance(kind, str) or not kind.startswith(_AUTOGROW_PREFIX):
                continue
            meta = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            template = meta.get("template") or {}
            names = [str(n) for n in (template.get("names") or [])]
            out[name] = names
    return out


def ensure_inputs_reach(workflow: dict, expect_refs: int = 0) -> None:
    """核对工作流里每个动态输入是否真能到达节点；不行就抛 SchemaError。

    expect_refs>0 表示这次是融合编辑，参考图槽位必须真实存在。
    ComfyUI 连不上时静默跳过（不阻断出图，只是没有这层保护）。
    """
    prompt = (workflow or {}).get("prompt") or {}
    problems: list[str] = []
    seen_refs = 0

    for node_id, node in prompt.items():
        class_type = (node or {}).get("class_type")
        inputs = (node or {}).get("inputs") or {}
        if not class_type:
            continue
        info = _object_info(str(class_type))
        if not info:
            continue
        slots = _autogrow_slots(info)
        for slot_name, allowed in slots.items():
            bare = slot_name in inputs
            used = [k for k in inputs if k.startswith(f"{slot_name}.")]
            if bare:
                problems.append(
                    f"节点 {node_id}（{class_type}）的输入 '{slot_name}' 是动态输入，"
                    f"必须写成 '{slot_name}.{allowed[0] if allowed else '<槽位名>'}' 这样的扁平键；"
                    f"写成嵌套字典会被 ComfyUI 静默丢弃，参考图将一张都不生效。"
                )
            if slot_name == "images":
                seen_refs = max(seen_refs, len(used))
            unknown = [k.split(".", 1)[1] for k in used
                       if allowed and k.split(".", 1)[1] not in allowed]
            if unknown:
                # 槽位名不在模板里通常仍可执行，但值得提示，避免拼错后静默失效
                problems.append(
                    f"节点 {node_id}（{class_type}）的 '{slot_name}' 用了模板外的槽位名 "
                    f"{unknown}（模板为 {allowed[:4]}…），可能不生效。"
                )

    if expect_refs > 0 and seen_refs == 0:
        problems.append(
            f"本次是融合编辑（{expect_refs} 张参考图），但工作流里没有任何 "
            f"'images.*' 输入 —— 参考图不会进入模型。"
        )

    if problems:
        raise SchemaError(
            "工作流与目标 ComfyUI 的节点契约不匹配，提交后会被静默丢弃：\n  - "
            + "\n  - ".join(problems)
            + f"\n（ComfyUI: {settings.comfyui_url}；可运行 scripts/check_workflow.py 离线复现）"
        )


def clear_cache() -> None:
    _INFO_CACHE.clear()
