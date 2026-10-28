# -*- coding: utf-8 -*-
"""控制图（ControlNet）通路探测。

为什么要探测而不是写死一条工作流
--------------------------------
Qwen-Image 2.1 的「姿态控制」有两条互不兼容的接法，取决于这台机器上到底
装了什么：

  A. Union 分支（推荐，用户说的「ControlNet Union int8」就是这条）
     comfyui/models/controlnet/Qwen-Image-2.1-Fun-Controlnet-Union-ComfyUI.safetensors
     + 插件 T8mars/Comfyui-Qwen-Image-2.1-Fun-Controlnet-Union-T8
     节点：QwenImage21UnionLoader → QwenImage21UnionApply(control_mode="Pose")
     一条 Union 权重覆盖 Canny/Depth/Pose/Lineart…，16 块分支，比整份
     世界级 ControlNet 轻，16G 卡跑得动。

  B. Control-LoRA（兜底，ComfyUI 内置节点就有）
     comfyui/models/model_patches/qwen_image_union_diffsynth_lora.safetensors
     节点：ModelPatchLoader → QwenImageDiffsynthControlnet

写死任何一条，另一种机器上就是「点了生成直接报错」或者更糟——静默退化成
没有控制。所以这里按 object_info 探测，探测不到就把「缺什么、放哪里」
说清楚，让用户知道为什么这帧没被骨架管住。
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.config import settings

UNION_LOADER = "QwenImage21UnionLoader"
UNION_APPLY = "QwenImage21UnionApply"
UNION_REF_ENCODE = "QwenImage21UnionReferenceEncode"
PATCH_LOADER = "ModelPatchLoader"
PATCH_APPLY = "QwenImageDiffsynthControlnet"

class ControlUnavailable(RuntimeError):
    """本机没有可用的控制通路（缺权重 / 缺节点 / 通道不支持）。

    调用方（流水线）靠这个标记判断「是不是可以降级」，而不是拿错误文本猜。
    """
    control_unavailable = True


UNION_DIR = "comfyui/models/controlnet"
PATCH_DIR = "comfyui/models/model_patches"


@dataclass
class ControlRoute:
    """一条可用的控制通路。"""
    route: str          # union | diffsynth
    model_name: str     # 交给加载节点的文件名（enum 里的值，不是绝对路径）
    detail: str         # 给日志 / 前端看的一句话

    @property
    def label(self) -> str:
        return "Union 分支" if self.route == "union" else "Control-LoRA"


def _names(info: dict | None, key: str) -> list[str]:
    """取加载节点某个下拉框的可选值（就是 ComfyUI 能看到的文件名）。"""
    req = ((info or {}).get("input") or {}).get("required") or {}
    spec = req.get(key)
    if isinstance(spec, (list, tuple)) and spec and isinstance(spec[0], list):
        return [str(n) for n in spec[0]]
    return []


def _pick(names: list[str], want: str, hint: str) -> str | None:
    if want and want in names:
        return want
    for n in names:
        if hint.lower() in n.lower():
            return n
    return None


def detect(object_info=None) -> tuple[ControlRoute | None, str]:
    """探测本机可用的控制通路。返回 (route, 说明)。

    object_info: 传 schema_guard.object_info 便于测试注入（避免真连 ComfyUI）。
    """
    if object_info is None:
        from engine.infer.schema_guard import object_info as _info
        object_info = _info

    apply_info = object_info(UNION_APPLY)
    loader_info = object_info(UNION_LOADER)
    if apply_info and loader_info:
        names = _names(loader_info, "union_model")
        picked = _pick(names, settings.control_union_file, "fun-controlnet-union")
        if picked:
            return (ControlRoute("union", picked,
                                 f"Qwen Image 2.1 Union 分支：{picked}"),
                    "")
        return (None,
                f"已装 Union 节点，但 {UNION_DIR}/ 里没有 Union 权重"
                f"（需要 Qwen-Image-2.1-Fun-Controlnet-Union-ComfyUI.safetensors）。")

    patch_info = object_info(PATCH_APPLY)
    patch_loader = object_info(PATCH_LOADER)
    if patch_info and patch_loader:
        names = _names(patch_loader, "name")
        picked = _pick(names, settings.control_lora_file, "union")
        if picked:
            return (ControlRoute("diffsynth", picked,
                                 f"Control-LoRA 兜底：{picked}"),
                    "")
        return (None,
                f"当前 ComfyUI 只有 Control-LoRA 节点，但 {PATCH_DIR}/ 里没有 "
                f"union LoRA（qwen_image_union_diffsynth_lora.safetensors）。")

    return (None,
            "当前 ComfyUI 没有任何 Qwen-Image 2.1 控制节点：装 "
            "Comfyui-Qwen-Image-2.1-Fun-Controlnet-Union-T8 插件，并把 "
            "Qwen-Image-2.1-Fun-Controlnet-Union-ComfyUI.safetensors 放到 "
            f"{UNION_DIR}/。")


def build_nodes(*, route: ControlRoute, model_link: list, vae_link: list,
                control_link: list, strength: float,
                start: float, end: float) -> tuple[dict, list]:
    """按通路的节点契约拼出「模型 + 控制图」这一段，返回 (节点表, 新模型链接)。"""
    nodes: dict = {}
    strength = max(0.0, min(2.0, float(strength)))
    start = max(0.0, min(1.0, float(start)))
    end = max(0.0, min(1.0, float(end)))
    if end < start:
        start, end = end, start

    if route.route == "union":
        nodes["30"] = {"class_type": UNION_LOADER, "inputs": {"union_model": route.model_name}}
        nodes["31"] = {"class_type": UNION_APPLY, "inputs": {
            "model": model_link,
            "union_patch": ["30", 0],
            "vae": vae_link,
            # 模式只是给读图的人看的标签，Union 读的是像素本身
            "control_mode": "Pose",
            "strength": strength,
            "start_percent": start,
            "end_percent": end,
            "control_image": control_link,
        }}
        return nodes, ["31", 0]

    nodes["30"] = {"class_type": PATCH_LOADER, "inputs": {"name": route.model_name}}
    nodes["31"] = {"class_type": PATCH_APPLY, "inputs": {
        "model": model_link,
        "model_patch": ["30", 0],
        "vae": vae_link,
        "image": control_link,
        "strength": strength,
    }}
    return nodes, ["31", 0]
