# -*- coding: utf-8 -*-
"""下载 Qwen-Image 2.1 所需的三个模型文件（约 14.6 GB，支持断点续传）。

用法（在项目根目录、已创建 .venv 的前提下）：

    # Windows
    .venv\\Scripts\\python.exe scripts\\download_models.py

    # Linux / macOS
    .venv/bin/python scripts/download_models.py

国内网络建议先设置镜像（脚本内已默认设置）：
    HF_ENDPOINT=https://hf-mirror.com

模型来源：https://huggingface.co/abenzerps/Qwen-Image-2.1-Uncensored-GGUF
"""
from __future__ import annotations

import os
from pathlib import Path

# 国内镜像，避免直连 huggingface.co 失败；如需走官方源请删除这一行
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from huggingface_hub import hf_hub_download  # noqa: E402

REPO_ID = "abenzerps/Qwen-Image-2.1-Uncensored-GGUF"

# 只下载默认档（Q4_K_M）。想换画质档请改这里的文件名 + .env 中 TRANSFORMER_FILE。
FILES = [
    "qwen-image-2.1-UC-Q4_K_M.gguf",                    # 扩散模型   4.60 GB
    "text_encoders/qwen3vl_8b_int8_convrot.safetensors",  # 文本编码器 9.35 GB
    "vae/qwen_image_2.1_vae_bf16.safetensors",          # VAE        0.68 GB
]


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    out = root / "models" / "qwen-image-2.1-unc"
    out.mkdir(parents=True, exist_ok=True)
    print(f"下载目录：{out}")
    print(f"镜像站点：{os.environ.get('HF_ENDPOINT')}\n")

    for name in FILES:
        print(f">>> {name}", flush=True)
        path = hf_hub_download(repo_id=REPO_ID, filename=name, local_dir=out)
        size = os.path.getsize(path) / 1024 ** 3
        print(f"    完成：{size:.2f} GB\n", flush=True)

    print("全部模型已就绪。接下来启动服务：start.bat（Windows）/ bash start.sh（Linux·macOS）")


if __name__ == "__main__":
    main()
