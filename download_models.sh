#!/usr/bin/env bash
# 下载 Qwen-Image 2.1 模型权重（约 14.6 GB，支持断点续传）
set -e
cd "$(dirname "$0")"

echo "============================================"
echo "  Download Qwen-Image 2.1 models (~14.6 GB)"
echo "============================================"

if [ ! -x ".venv/bin/python" ]; then
  echo "[错误] 未找到 .venv/bin/python，请先执行一次 bash start.sh 创建环境。"
  exit 1
fi

export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
.venv/bin/python scripts/download_models.py

echo
echo "完成。若中途报错，重新执行本脚本即可续传。"
