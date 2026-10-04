#!/usr/bin/env bash
# 本地图片生成器 · Qwen-Image 2.1 启动脚本
# Linux / macOS 原生使用；Windows（Git Bash）下自动转调 start.bat

set -e
cd "$(dirname "$0")"

# ---------- Windows: 转调 bat，避免走 Linux 路径 ----------
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    echo "[提示] 检测到 Windows 环境，转用 start.bat 启动 ..."
    exec cmd //c start.bat
    ;;
esac

echo "============================================"
echo "  本地图片生成器 · Qwen-Image 2.1"
echo "============================================"

PYTHON_BIN="${PYTHON_BIN:-python3}"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || { echo "[错误] 未找到 $PYTHON_BIN"; exit 1; }

if [ ! -x ".venv/bin/python" ]; then
  echo "[1/4] 创建虚拟环境 .venv ..."
  "$PYTHON_BIN" -m venv .venv
else
  echo "[1/4] 虚拟环境已存在，跳过。"
fi

PY=".venv/bin/python"

if [ ! -f ".venv/.deps_ok" ]; then
  echo "[2/4] 安装 Web 依赖 ..."
  "$PY" -m pip install --upgrade pip -q
  "$PY" -m pip install -r backend/requirements.txt -q
  touch ".venv/.deps_ok"
else
  echo "[2/4] Web 依赖已安装，跳过。"
fi

if [ ! -f ".env" ]; then
  echo "[3/4] 生成 .env ..."
  cp .env.example .env
else
  echo "[3/4] .env 已存在，跳过。"
fi

# 端口检查
PORT=$(grep -E '^PORT=' .env 2>/dev/null | head -1 | cut -d= -f2 | tr -d ' \r')
PORT=${PORT:-8000}
if command -v lsof >/dev/null 2>&1 && lsof -i :"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "[警告] 端口 $PORT 已被占用（PID: $(lsof -i :$PORT -sTCP:LISTEN -t | head -1)）"
  echo "       请先停止该进程，或修改 .env 中的 PORT"
  exit 1
elif command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  echo "[警告] 端口 $PORT 已被占用，请先停止该进程，或修改 .env 中的 PORT"
  exit 1
fi

echo "[4/4] 启动服务 ..."
echo
echo "  打开浏览器访问： http://127.0.0.1:$PORT"
echo "  停止服务：按 Ctrl+C"
echo

cd backend
exec "../$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
