# -*- coding: utf-8 -*-
"""工作流自检：不跑推理，也不占用显存，只回答一个问题——

「我们提交给 ComfyUI 的每个节点输入，真的会到达节点吗？」

为什么需要这个脚本
------------------
ComfyUI 的 API 格式对**动态输入**（Autogrow / DynamicCombo / DynamicSlot）有
一套容易被忽略的命名规则：真名是父输入名 + "." + 槽位名，例如
`images.image_1`。写成嵌套字典 `images={"image_1": [...]}` 时，ComfyUI 展开
schema 后根本找不到名为 `images` 的输入，`execution.get_input_data()` 会**静默
丢弃**它——节点收到 `images=None`，参考图一张都没进模型，编辑退化成文生图，
不报错、不告警，只是每张图各画一个人。

这类错误不可能靠看代码发现（两种写法都"看起来很对"），mock 引擎也发现不了
（mock 不做 schema 校验）。所以这里直接借用 ComfyUI 自己的解析函数来对齐：

    _io.get_finalized_class_inputs(node.INPUT_TYPES(), inputs)  → 展开后的 schema
    comfy_execution.graph.get_input_info(class, key, schema)    → 这个键还在不在

用法（开发机上，不启动 ComfyUI）：

    python scripts/check_workflow.py

退出码 0 = 全部输入都能到达节点；1 = 有输入会被静默丢弃。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMFY = ROOT / "comfyui"

# 工作流里会用到的全部节点；缺一个就说明本机 comfyui 目录不完整
CORE = ("KSampler", "CLIPLoader", "VAELoader", "LoadImage", "CLIPTextEncode",
        "VAEDecode", "VAEEncode", "SaveImage")


def _load_schema_providers():
    """把 comfyui 的节点类捞出来，返回 {class_type: class}。"""
    if not (COMFY / "comfy_extras").is_dir():
        raise SystemExit(f"找不到 ComfyUI 源码：{COMFY}\n"
                         "自检需要 comfyui/ 目录（只用它的 schema，不加载权重）。")
    sys.path.insert(0, str(COMFY))

    import nodes  # noqa: E402  ComfyUI 的核心节点
    from comfy_extras import nodes_model_advanced, nodes_qwen, nodes_sd3  # noqa: E402

    table = {name: nodes.NODE_CLASS_MAPPINGS[name] for name in CORE
             if name in nodes.NODE_CLASS_MAPPINGS}
    table["TextEncodeQwenImage21"] = nodes_qwen.TextEncodeQwenImage21
    table["ModelSamplingAuraFlow"] = nodes_model_advanced.ModelSamplingAuraFlow
    table["EmptySD3LatentImage"] = nodes_sd3.EmptySD3LatentImage

    # GGUF 加载器在 custom_nodes 里（带相对导入，必须按包加载）
    try:
        import importlib.util

        pkg = COMFY / "custom_nodes" / "ComfyUI-GGUF"
        if (pkg / "__init__.py").exists():
            spec = importlib.util.spec_from_file_location(
                "ComfyUI_GGUF", pkg / "__init__.py",
                submodule_search_locations=[str(pkg)])
            mod = importlib.util.module_from_spec(spec)
            sys.modules["ComfyUI_GGUF"] = mod
            spec.loader.exec_module(mod)
            table["UnetLoaderGGUF"] = mod.NODE_CLASS_MAPPINGS["UnetLoaderGGUF"]
    except Exception as exc:  # pragma: no cover - 环境相关
        print(f"[skip] UnetLoaderGGUF 无法离线导入（{type(exc).__name__}: {exc}），跳过其 schema 校验")
        table.setdefault("UnetLoaderGGUF", None)

    missing = [n for n in CORE if n not in table]
    if missing:
        raise SystemExit(f"ComfyUI 节点表缺少：{missing}")
    return table


def _accepted_inputs(cls, inputs: dict) -> tuple[set[str], dict]:
    """这个节点最终会接受哪些输入键（其余键会被 execution 静默丢掉）。"""
    from comfy_api.latest import _io

    raw = cls.INPUT_TYPES()
    try:
        valid, _hidden, v3 = _io.get_finalized_class_inputs(raw, inputs)
        keys = set()
        for cat in ("required", "optional", "hidden"):
            keys |= set((valid.get(cat) or {}).keys())
        return keys, v3
    except Exception:
        keys = set()
        for cat in ("required", "optional", "hidden"):
            keys |= set((raw.get(cat) or {}).keys())
        return keys, {}


def check(name: str, workflow: dict, table: dict) -> list[str]:
    """返回问题清单；空列表 = 通过。"""
    problems: list[str] = []
    nodes = workflow["prompt"]
    for nid, node in nodes.items():
        ctype = node["class_type"]
        if ctype not in table:
            problems.append(f"{name}: 节点 {nid} 的类型 {ctype} 不在本机 ComfyUI 里")
            continue
        cls = table[ctype]
        if cls is None:      # 明确标记为「无法离线校验」，不算失败
            continue
        inputs = node.get("inputs") or {}
        accepted, v3 = _accepted_inputs(cls, inputs)
        for key in inputs:
            if key in accepted:
                continue
            paths = (v3 or {}).get("dynamic_paths") or {}
            hint = ""
            if key in paths:  # 常见错法：把动态输入写成嵌套字典
                hint = (f"\n      ↳ '{key}' 是动态输入的父名，API 格式必须展开成扁平键，"
                        f"例如 '{key}.<槽位名>' = [节点, 输出序号]")
            problems.append(f"{name}: 节点 {nid}({ctype}) 的输入 '{key}' 会被静默丢弃{hint}")

        # 融合节点：参考图必须登记为动态槽位，否则等于没传
        if ctype == "TextEncodeQwenImage21":
            paths = (v3 or {}).get("dynamic_paths") or {}
            slots = [k for k in accepted if k.startswith("images.")]
            live = [k for k in inputs if k.startswith("images.")]
            if not live:
                problems.append(f"{name}: TextEncodeQwenImage21 没有挂任何参考图（images.image_N）")
            for k in live:
                if k not in paths:
                    problems.append(f"{name}: 参考图槽位 '{k}' 未被 ComfyUI 登记为动态输入")
            if slots and not live:
                problems.append(f"{name}: 节点有 {len(slots)} 个可用槽位，实际一个都没用")
    return problems


def main() -> int:
    table = _load_schema_providers()
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    # 直接把引擎真实产出接出来：三张假图足够触发 img2img / fusion 两条分支
    from engine.infer.comfy_engine import ComfyEngine

    eng = ComfyEngine.__new__(ComfyEngine)          # 不连 ComfyUI，只借 _build_workflow
    eng.client_id = "selfcheck"

    cases = {
        "txt2img": dict(prompt="团子站在门口", negative="", width=832, height=1216,
                        steps=20, guidance_scale=4.0, seed=1,
                        ref_name=None, strength=1.0, ref_names=None),
        "img2img": dict(prompt="团子站在门口", negative="", width=832, height=1216,
                        steps=20, guidance_scale=4.0, seed=1,
                        ref_name="ref.png", strength=0.6, ref_names=None),
        "fusion-1": dict(prompt="编辑 image_1", negative="", width=832, height=1216,
                         steps=20, guidance_scale=1.0, seed=1,
                         ref_name=None, strength=1.0, ref_names=["cast.png"]),
        "fusion-2": dict(prompt="编辑 image_1", negative="", width=832, height=1216,
                         steps=20, guidance_scale=1.0, seed=1,
                         ref_name=None, strength=1.0,
                         ref_names=["cast.png", "scene.png"]),
    }

    all_problems: list[str] = []
    for name, kwargs in cases.items():
        wf = eng._build_workflow(**kwargs)
        probs = check(name, wf, table)
        all_problems += probs
        status = "OK" if not probs else f"{len(probs)} 个问题"
        print(f"[{name:8}] 节点 {len(wf['prompt']):2} 个 → {status}")
        if name.startswith("fusion"):
            enc = next(n for n in wf["prompt"].values()
                       if n["class_type"] == "TextEncodeQwenImage21")
            ref = {k: v for k, v in enc["inputs"].items() if k.startswith("images.")}
            print(f"           参考图接线：{json.dumps(ref, ensure_ascii=False)}")

    if all_problems:
        print("\n发现问题：")
        for p in all_problems:
            print("  -", p)
        return 1
    print("\n全部通过：每个输入键都能到达节点，参考图已登记为动态槽位。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
