# -*- coding: utf-8 -*-
"""显存 / 设备探测工具。

在未安装 torch 的环境下（例如纯 mock 模式）也能安全调用，
统一返回可用性标记，避免到处写 try/except。
"""

from __future__ import annotations

from typing import Optional

try:  # pragma: no cover - 取决于运行环境
    import torch

    TORCH_AVAILABLE = True
except Exception:  # pragma: no cover
    torch = None  # type: ignore
    TORCH_AVAILABLE = False


def cuda_available() -> bool:
    return bool(TORCH_AVAILABLE and torch.cuda.is_available())


def vram_info_gb() -> tuple[Optional[float], Optional[float]]:
    """返回 (总显存, 剩余显存)，单位 GB。不可用时返回 (None, None)。"""
    if not cuda_available():
        return None, None
    try:
        free, total = torch.cuda.mem_get_info()
        return round(total / 1024 ** 3, 2), round(free / 1024 ** 3, 2)
    except Exception:
        return None, None


def free_vram_gb() -> float:
    _, free = vram_info_gb()
    return free or 0.0


def empty_cache() -> None:
    if cuda_available():
        try:
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
        except Exception:
            pass


def gpu_name() -> Optional[str]:
    if not cuda_available():
        return None
    try:
        return torch.cuda.get_device_name(0)
    except Exception:
        return None


def resolve_dtype(name: str):
    """把配置里的精度字符串解析为 torch.dtype；torch 不可用时返回字符串。"""
    if not TORCH_AVAILABLE:
        return name
    mapping = {
        "float16": torch.float16,
        "fp16": torch.float16,
        "bfloat16": torch.bfloat16,
        "bf16": torch.bfloat16,
        "float32": torch.float32,
        "fp32": torch.float32,
    }
    return mapping.get((name or "").lower(), torch.float16)
