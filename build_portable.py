# -*- coding: utf-8 -*-
"""绿色便携包构建脚本。

产出 dist/qwenimages-portable/：
  - 应用代码（shared / engine / apps / portal / comfyui）
  - 内嵌 Python 3.13 运行时 + 全部依赖（site-packages 合并进运行时）
  - 启动脚本（解压即用，零安装）
  - 模型下载脚本（模型权重 14G 不打进包，由用户首次运行时下载）

用法： ./.venv/Scripts/python.exe build_portable.py [输出目录]
     不传输出目录时产出 dist/qwenimages-portable/；
     传目录时直接构建到该目录（不压缩），例如：
       ./.venv/Scripts/python.exe build_portable.py F:/qwenimage
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent
STAGE = ROOT / "dist" / "qwenimages-portable"
# 基础 Python 运行时（含全部依赖的 site-packages 所在环境）：
# 默认取当前解释器环境，可用环境变量 PY_RUNTIME_SRC 覆盖
PY_RUNTIME_SRC = Path(os.environ.get("PY_RUNTIME_SRC", sys.base_prefix))
VENV_SITE = ROOT / ".venv" / "Lib" / "site-packages"

# ffmpeg 来源：开发机上现成的分发版目录（ffmpeg.exe + 一串 dll）。
# 可用环境变量 FFMPEG_SRC 覆盖；找不到就跳过（便携包退回「用户自备 ffmpeg」）。
FFMPEG_SRC = Path(os.environ.get("FFMPEG_SRC", r"D:\AI\ffmpeg\bin"))

ROBOCOPY_OK = (0, 1, 2, 3)

# robocopy 的「停滞判定」：连续这么久没有新的读写字节，就认为它死锁了。
# 现象：/MT 多线程 + stdout 重定向时，robocopy 偶尔会卡在某个句柄上不退出
# （CPU 0%、I/O 0、系统整体空闲），此时只能杀掉并换单线程重跑。
STALL_SECONDS = 150
ROBOCOPY_LOG = ROOT / "data" / "_robocopy.log"


def _write(path: Path, data: str, encoding: str = "utf-8", errors: str = "strict") -> None:
    """写文件（自动建父目录）。

    robocopy 刚写完的文件可能被杀软/索引短暂占用，Windows 下会抛 PermissionError，
    这里重试几次；编码错误按调用方要求保持严格（bat 必须 ASCII）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for _ in range(8):
        try:
            path.write_text(data, encoding=encoding, errors=errors)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(0.8)
    raise SystemExit(f"写入失败（重试后仍被拒绝）：{path}\n  {last}")


def _proc_io(pid: int) -> int:
    """进程累计读写字节；取不到时返回 -1（视为无变化）。"""
    try:
        io = psutil.Process(pid).io_counters()
        return io.read_bytes + io.write_bytes
    except Exception:
        return -1


def robocopy(src: Path, dst: Path, *, exclude_dirs: list[str] | None = None,
             exclude_files: list[str] | None = None, threads: int = 1) -> None:
    """复制目录树（带停滞看门狗）。

    输出重定向到日志文件（不用 PIPE）以避免 /MT 下的管道死锁；
    若进程在 STALL_SECONDS 内没有任何 I/O 增长，则杀掉并用单线程重跑一次。
    """
    base = ["robocopy", str(src), str(dst), "/E", "/NFL", "/NDL", "/NJH", "/R:1", "/W:1"]
    for d in exclude_dirs or []:
        base += ["/XD", str(src / d), d]
    for f in exclude_files or []:
        base += ["/XF", f]

    ROBOCOPY_LOG.parent.mkdir(parents=True, exist_ok=True)
    last_rc, last_stalled = None, False
    for attempt, th in enumerate((threads, 1)):
        cmd = base + ([f"/MT:{th}"] if th > 1 else [])
        if attempt:
            print(f"  改用单线程重跑：{src.name} -> {dst}")
        with open(ROBOCOPY_LOG, "wb") as fp:
            proc = subprocess.Popen(cmd, stdout=fp, stderr=subprocess.STDOUT)
            last_io, last_t = _proc_io(proc.pid), time.time()
            stalled = False
            while proc.poll() is None:
                time.sleep(10)
                cur = _proc_io(proc.pid)
                if cur != last_io:
                    last_io, last_t = cur, time.time()
                elif time.time() - last_t > STALL_SECONDS:
                    stalled = True
                    proc.kill()
                    proc.wait()
                    break
        rc = proc.returncode
        if not stalled and rc in ROBOCOPY_OK:
            return
        last_rc, last_stalled = rc, stalled
        if not attempt:
            reason = "停滞（看门狗触发）" if stalled else f"返回码 {rc}"
            print(f"  robocopy {reason}，重试中 ...")
            continue
        print(ROBOCOPY_LOG.read_bytes().decode("gbk", "replace")[-2000:])
        raise SystemExit(
            f"robocopy 失败（{'停滞' if last_stalled else last_rc}）: {src} -> {dst}")


def _partial_build(p: Path) -> bool:
    """是否为「上一次中断的便携包构建残留」：有应用代码但没有 .venv / 模型权重。"""
    return ((p / "backend" / "app" / "main.py").exists()
            and not (p / ".venv").exists()
            and not (p / "models" / "qwen-image-2.1-unc" / "qwen-image-2.1-UC-Q4_K_M.gguf").exists())


def main() -> None:
    global STAGE
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    clean = "--clean" in sys.argv[1:]
    if args:
        STAGE = Path(args[0]).expanduser().resolve()
    if not PY_RUNTIME_SRC.exists():
        raise SystemExit(f"找不到基础 Python 运行时: {PY_RUNTIME_SRC}")

    print(f"输出目录：{STAGE}")
    if STAGE.exists():
        entries = list(STAGE.iterdir())
        ours = ((STAGE / "start.bat").exists() or (STAGE / "runtime").exists()
                or _partial_build(STAGE))
        if entries and not ours:
            raise SystemExit(
                f"目标目录非空且不像本项目的便携包，已中止以免误删：{STAGE}\n"
                f"（如需强制覆盖，请先自行清空该目录）"
            )
        if entries and clean:
            # 默认不删：robocopy /E 会原地覆盖，避免大范围删除触发安全确认
            print("清理旧的便携包目录（--clean）...")
            shutil.rmtree(STAGE)
        elif entries:
            print("目标目录已有内容：原地覆盖构建（如需先清空请加 --clean）")
    STAGE.mkdir(parents=True, exist_ok=True)

    print("[1/8] 应用代码（四层：shared / engine / apps / portal）...")
    for pkg in ("shared", "engine", "apps", "portal"):
        robocopy(ROOT / pkg, STAGE / pkg, exclude_dirs=["__pycache__"],
                 exclude_files=["*.pyc"])
    robocopy(ROOT / "backend", STAGE / "backend", exclude_dirs=["__pycache__", "tests", ".workbuddy"],
             exclude_files=["*.pyc"])
    robocopy(ROOT / "scripts", STAGE / "scripts", exclude_dirs=["__pycache__"])
    robocopy(ROOT / "comfyui", STAGE / "comfyui",
             # 注意：input/user/output/temp 只排除 comfyui 顶层的运行时目录
             # （用绝对路径），不能用裸名——否则会误删 comfy_api/input 等包目录
             exclude_dirs=[".git", "__pycache__",
                           str(ROOT / "comfyui" / "user"),
                           str(ROOT / "comfyui" / "output"),
                           str(ROOT / "comfyui" / "input"),
                           str(ROOT / "comfyui" / "temp")],
             exclude_files=["*.pyc", "*.log"])
    (STAGE / "comfyui" / "user").mkdir(parents=True, exist_ok=True)
    (STAGE / "comfyui" / "output").mkdir(parents=True, exist_ok=True)
    (STAGE / "comfyui" / "input").mkdir(parents=True, exist_ok=True)

    print("[2/8] 修正 ComfyUI 模型路径为相对路径 ...")
    _write(STAGE / "comfyui" / "extra_model_paths.yaml", 
        "# 复用便携包根目录的 models/（相对本文件解析）\n"
        "qwenimages:\n"
        "    base_path: ../models\n"
        "    is_default: true\n"
        "    diffusion_models: qwen-image-2.1-unc\n"
        "    text_encoders: qwen-image-2.1-unc/text_encoders\n"
        "    vae: qwen-image-2.1-unc/vae\n",
        encoding="utf-8",
    )

    print("[3/8] Python 运行时 ...")
    robocopy(PY_RUNTIME_SRC, STAGE / "runtime" / "python",
             exclude_dirs=["__pycache__", "Scripts"], threads=8)
    # python.exe 必须在，Scripts 只含 pip 等入口（venv 里的会覆盖，不要）

    print("[4/8] 全部依赖 site-packages（约 5.4G，已存在的文件会自动跳过）...")
    robocopy(VENV_SITE, STAGE / "runtime" / "python" / "Lib" / "site-packages",
             exclude_dirs=["__pycache__"], exclude_files=["*.pyc"], threads=8)

    print("[5/8] 目录骨架 ...")
    (STAGE / "models" / "qwen-image-2.1-unc" / "text_encoders").mkdir(parents=True, exist_ok=True)
    (STAGE / "models" / "qwen-image-2.1-unc" / "vae").mkdir(parents=True, exist_ok=True)
    (STAGE / "data").mkdir(exist_ok=True)
    (STAGE / "scripts").mkdir(exist_ok=True)

    print("[6/8] 外部工具（ffmpeg + 旁白服务）...")
    bundle_ffmpeg()
    robocopy(ROOT / "tools", STAGE / "tools", exclude_dirs=["__pycache__", "index-tts"])
    # tools/index-tts 是 IndexTTS 的仓库克隆（几 GB 权重），由 install_tts.bat 装进
    # 便携包，绝不从开发机带过来——它必须用 3.10/3.11 的独立 venv 重建

    print("[7/8] 生成启动脚本与文档 ...")
    write_scripts()

    print("[8/8] 统计体积 ...")
    total = sum(f.stat().st_size for f in STAGE.rglob("*") if f.is_file())
    print(f"完成：{STAGE}")
    print(f"总体积：{total / 1024 ** 3:.2f} GB")


def bundle_ffmpeg() -> None:
    """把 ffmpeg 分发版拷进 runtime/ffmpeg/。

    只拷 exe + dll，不拷 doc/include/lib（那三样占的空间比本体还大）。
    拷完必须真的跑一次 -version：ffmpeg.exe 才 0.5MB，真正干活的是那串
    dll，少一个就起不来——存在 ≠ 可用。
    """
    dst = STAGE / "runtime" / "ffmpeg"
    dst.mkdir(parents=True, exist_ok=True)
    if not FFMPEG_SRC.exists():
        print(f"  [WARN] 未找到 ffmpeg 来源目录 {FFMPEG_SRC}，便携包将不含 ffmpeg"
              "（渲染成片时需要用户自备，见 .env 里 FFMPEG_PATH）")
        return
    copied = 0
    for pattern in ("*.exe", "*.dll"):
        for src in FFMPEG_SRC.glob(pattern):
            if src.name.lower() == "ffplay.exe":
                continue        # 播放器，12MB，便携包用不上
            shutil.copy2(src, dst / src.name)
            copied += 1
    try:
        r = subprocess.run([str(dst / "ffmpeg.exe"), "-version"],
                           capture_output=True, timeout=30)
        ok = r.returncode == 0 and b"ffmpeg" in (r.stdout or b"").lower()
    except Exception:
        ok = False
    if not ok:
        raise SystemExit(f"ffmpeg 拷贝后无法运行（缺 dll？）：{dst}")
    size = sum(f.stat().st_size for f in dst.iterdir()) / 1024 / 1024
    print(f"  ffmpeg 就绪（{copied} 个文件，{size:.0f} MB）")


def write_scripts() -> None:
    """start.bat 必须纯 ASCII（cmd 按 GBK 解析，中文会破坏命令）。"""
    _write(STAGE / "start.bat", 
        "@echo off\r\n"
        "setlocal\r\n"
        "title QwenImages Portable\r\n"
        "cd /d \"%~dp0\"\r\n"
        "\r\n"
        "if not exist \"runtime\\python\\python.exe\" (\r\n"
        "  echo [ERROR] runtime\\python\\python.exe not found. Package may be incomplete.\r\n"
        "  pause\r\n"
        "  exit /b 1\r\n"
        ")\r\n"
        "\r\n"
        "set \"PATH=%~dp0runtime\\python;%PATH%\"\r\n"
        "\r\n"
        "if not exist \".env\" (\r\n"
        "  echo [1/2] First run: creating .env ...\r\n"
        "  copy /y .env.example .env >nul\r\n"
        ") else (\r\n"
        "  echo [1/2] .env found.\r\n"
        ")\r\n"
        "\r\n"
        "rem ---- read PORT from .env (default 8000) ----\r\n"
        "set PORT=8000\r\n"
        "for /f \"tokens=1,* delims==\" %%a in ('findstr /b /i \"PORT=\" \".env\" 2^>nul') do set PORT=%%b\r\n"
        "\r\n"
        "rem ---- port pre-check ----\r\n"
        "netstat -ano | findstr /R /C:\":%PORT% .*LISTENING\" >nul 2>&1\r\n"
        "if not errorlevel 1 (\r\n"
        "  echo [WARN] Port %PORT% is already in use. Close the other instance first,\r\n"
        "  echo        or change PORT in .env\r\n"
        "  pause\r\n"
        "  exit /b 1\r\n"
        ")\r\n"
        "\r\n"
        "echo [2/2] Starting server ...\r\n"
        "echo.\r\n"
        "echo   Open in browser:  http://127.0.0.1:%PORT%\r\n"
        "echo   First launch will auto-start ComfyUI (~45s) and load models on demand.\r\n"
        "echo.\r\n"
        "\r\n"
        "\"runtime\\python\\python.exe\" -m uvicorn portal.backend.main:app --host 127.0.0.1 --port %PORT%\r\n"
        "echo.\r\n"
        "echo Server exited unexpectedly. Read the error above.\r\n"
        "pause\r\n",
        encoding="ascii", errors="strict",
    )

    _write(STAGE / "run_platform.bat", 
        "@echo off\r\n"
        "rem Platform mode: portal + all apps + engine in ONE process.\r\n"
        "rem Same as start.bat (port check + .env bootstrap included).\r\n"
        "cd /d \"%~dp0\"\r\n"
        "call start.bat\r\n",
        encoding="ascii", errors="strict",
    )

    _write(STAGE / "run_app.bat", 
        "@echo off\r\n"
        "setlocal\r\n"
        "cd /d \"%~dp0\"\r\n"
        "\r\n"
        "rem Run ONE app standalone (no portal, engine in that process)\r\n"
        "set APPID=%~1\r\n"
        "if \"%APPID%\"==\"\" (\r\n"
        "  echo Usage: run_app.bat ^<imagegen^|comic^|portrait^|dango^|storyboard^>\r\n"
        "  pause\r\n"
        "  exit /b 1\r\n"
        ")\r\n"
        "if not exist \"apps\\%APPID%\\manifest.json\" (\r\n"
        "  echo [ERROR] unknown app: %APPID%\r\n"
        "  pause\r\n"
        "  exit /b 1\r\n"
        ")\r\n"
        "if not exist \"runtime\\python\\python.exe\" (\r\n"
        "  echo [ERROR] runtime not found.\r\n"
        "  pause\r\n"
        "  exit /b 1\r\n"
        ")\r\n"
        "\r\n"
        "set PORT=8101\r\n"
        "if /i \"%APPID%\"==\"comic\"      set PORT=8102\r\n"
        "if /i \"%APPID%\"==\"portrait\"   set PORT=8103\r\n"
        "if /i \"%APPID%\"==\"dango\"      set PORT=8104\r\n"
        "if /i \"%APPID%\"==\"storyboard\" set PORT=8105\r\n"
        "\r\n"
        "echo [app:%APPID%] open http://127.0.0.1:%PORT%\r\n"
        "\"runtime\\python\\python.exe\" -m uvicorn apps.%APPID%.backend.routes:create_app --factory --host 127.0.0.1 --port %PORT%\r\n"
        "echo.\r\n"
        "echo Server stopped.\r\n"
        "pause\r\n",
        encoding="ascii", errors="strict",
    )

    _write(STAGE / "run_engine.bat", 
        "@echo off\r\n"
        "setlocal\r\n"
        "cd /d \"%~dp0\"\r\n"
        "\r\n"
        "rem Engine as its own process. Clients need ENGINE_TRANSPORT=http\r\n"
        "rem and ENGINE_URL=http://127.0.0.1:8189 in .env\r\n"
        "if not exist \"runtime\\python\\python.exe\" (\r\n"
        "  echo [ERROR] runtime not found.\r\n"
        "  pause\r\n"
        "  exit /b 1\r\n"
        ")\r\n"
        "\r\n"
        "echo [engine] status: http://127.0.0.1:8189/engine/v1/status\r\n"
        "\"runtime\\python\\python.exe\" -m uvicorn engine.serve:app --host 127.0.0.1 --port 8189\r\n"
        "echo.\r\n"
        "echo Engine stopped.\r\n"
        "pause\r\n",
        encoding="ascii", errors="strict",
    )

    _write(STAGE / "run_tts.bat",
        "@echo off\r\n"
        "setlocal\r\n"
        "title Narration worker (IndexTTS)\r\n"
        "cd /d \"%~dp0\"\r\n"
        "\r\n"
        "rem Narration worker. Default backend is edge-tts: no weights, no VRAM,\r\n"
        "rem only needs internet. If you ran install_tts.bat, its Python 3.11 venv\r\n"
        "rem is picked up automatically and the local IndexTTS model is used.\r\n"
        "\r\n"
        "set PY=runtime\\python\\python.exe\r\n"
        "if exist \"tools\\index-tts\\.venv\\Scripts\\python.exe\" (\r\n"
        "  set PY=tools\\index-tts\\.venv\\Scripts\\python.exe\r\n"
        "  echo [tts] using IndexTTS venv ^(local model^)\r\n"
        ") else (\r\n"
        "  echo [tts] using edge-tts ^(no model, needs internet^)\r\n"
        ")\r\n"
        "if not exist \"%PY%\" (\r\n"
        "  echo [ERROR] no python found\r\n"
        "  pause\r\n"
        "  exit /b 1\r\n"
        ")\r\n"
        "\r\n"
        "echo [tts] http://127.0.0.1:8190  (close this window to stop)\r\n"
        "\"%PY%\" tools\\tts_worker\\server.py --port 8190 --home tools\\index-tts --models checkpoints_2 --version 2 --backend auto\r\n"
        "echo.\r\n"
        "echo TTS worker stopped.\r\n"
        "pause\r\n",
        encoding="ascii", errors="strict",
    )

    _write(STAGE / "check.bat",
        "@echo off\r\n"
        "setlocal\r\n"
        "title Workflow self-check\r\n"
        "cd /d \"%~dp0\"\r\n"
        "\r\n"
        "rem Answers one question: will reference images actually reach the model?\r\n"
        "rem Replacing runtime\\ or any Scripts folder never changes this - the logic\r\n"
        "rem lives in engine\\ and shared\\. This script reads the code and reports which\r\n"
        "rem build it is, then validates the workflow against ComfyUI's own schema.\r\n"
        "rem Takes ~5 seconds, no GPU, no model loading.\r\n"
        "\r\n"
        "if not exist \"runtime\\python\\python.exe\" (\r\n"
        "  echo [ERROR] runtime\\python\\python.exe not found\r\n"
        "  pause\r\n"
        "  exit /b 1\r\n"
        ")\r\n"
        "\r\n"
        "set PYTHONIOENCODING=utf-8\r\n"
        "\"runtime\\python\\python.exe\" scripts\\check_workflow.py\r\n"
        "echo.\r\n"
        "pause\r\n")

    _write(STAGE / "install_tts.bat",
        "@echo off\r\n"
        "setlocal\r\n"
        "cd /d \"%~dp0\"\r\n"
        "\r\n"
        "rem OPTIONAL: high-fidelity offline narration with IndexTTS-2 (~5.5 GB).\r\n"
        "rem The default backend (edge-tts) already produces real voices without\r\n"
        "rem this - no model, no VRAM. Run this only if you want the local model.\r\n"
        "rem   git clone + uv sync (Python 3.11 venv) + model download.\r\n"
        "rem Safe to re-run: every step is skipped when already done.\r\n"
        "\r\n"
        "where git >nul 2>&1\r\n"
        "if errorlevel 1 (\r\n"
        "  echo [ERROR] git not found. Install it from https://git-scm.com first.\r\n"
        "  pause\r\n"
        "  exit /b 1\r\n"
        ")\r\n"
        "where uv >nul 2>&1\r\n"
        "if errorlevel 1 (\r\n"
        "  echo [1/3] Installing uv ...\r\n"
        "  runtime\\python\\python.exe -m pip install -U uv\r\n"
        ")\r\n"
        "\r\n"
        "if not exist \"tools\\index-tts\\pyproject.toml\" (\r\n"
        "  rem A half-finished clone leaves a .git folder behind; wipe it first.\r\n"
        "  if exist \"tools\\index-tts\" rd /s /q \"tools\\index-tts\"\r\n"
        "  echo [2/3] Cloning index-tts ...\r\n"
        "  if not exist \"tools\" mkdir tools\r\n"
        "  git clone --depth 1 https://github.com/index-tts/index-tts.git tools\\index-tts\r\n"
        ")\r\n"
        "\r\n"
        "pushd tools\\index-tts\r\n"
        "if not exist \".venv\\Scripts\\python.exe\" (\r\n"
        "  echo [2/3] Creating venv + installing deps ^(this can take 10+ minutes^) ...\r\n"
        "  uv sync --all-extras --default-index https://mirrors.aliyun.com/pypi/simple\r\n"
        "  if errorlevel 1 ( popd & echo [ERROR] uv sync failed & pause & exit /b 1 )\r\n"
        ")\r\n"
        "if not exist \"checkpoints_2\\config.yaml\" (\r\n"
        "  echo [3/3] Downloading IndexTTS-2 weights ~5.5 GB ^(resumable^) ...\r\n"
        "  set HF_ENDPOINT=https://hf-mirror.com\r\n"
        "  uv tool install huggingface-hub 2>nul\r\n"
        "  uv tool run --from huggingface-hub hf download IndexTeam/IndexTTS-2 --local-dir=checkpoints_2\r\n"
        "  if errorlevel 1 ( popd & echo [ERROR] download failed, re-run to resume & pause & exit /b 1 )\r\n"
        ")\r\n"
        "popd\r\n"
        "echo.\r\n"
        "echo Done. Narration now uses the real IndexTTS model.\r\n"
        "echo Start it with run_tts.bat, or let the portal auto-start it.\r\n"
        "pause\r\n",
        encoding="ascii", errors="strict",
    )

    _write(STAGE / "download_models.bat", 
        "@echo off\r\n"
        "setlocal\r\n"
        "cd /d \"%~dp0\"\r\n"
        "if not exist \"runtime\\python\\python.exe\" (\r\n"
        "  echo [ERROR] runtime not found.\r\n"
        "  pause\r\n"
        "  exit /b 1\r\n"
        ")\r\n"
        "set \"HF_ENDPOINT=https://hf-mirror.com\"\r\n"
        "\"runtime\\python\\python.exe\" scripts\\download_models.py\r\n"
        "echo.\r\n"
        "echo Done. If there were errors above, run this again (resumable).\r\n"
        "pause\r\n",
        encoding="ascii", errors="strict",
    )

    # 仓库里的 scripts/download_models.py 才是权威版本（注释更全、与 .env 联动更细）；
    # 构建时已由 robocopy 带过来，这里只在缺失时补一个最小实现兜底，避免覆盖。
    if not (STAGE / "scripts" / "download_models.py").exists():
        _write(STAGE / "scripts" / "download_models.py",
            "# -*- coding: utf-8 -*-\n"
            '"""Download the three required model files (~14.6 GB, resumable)."""\n'
            "import os\n"
            "from pathlib import Path\n"
            "\n"
            "from huggingface_hub import hf_hub_download\n"
            "\n"
            "REPO = 'abenzerps/Qwen-Image-2.1-Uncensored-GGUF'\n"
            "FILES = [\n"
            "    'qwen-image-2.1-UC-Q4_K_M.gguf',\n"
            "    'text_encoders/qwen3vl_8b_int8_convrot.safetensors',\n"
            "    'vae/qwen_image_2.1_vae_bf16.safetensors',\n"
            "]\n"
            "\n"
            "def main():\n"
            "    root = Path(__file__).resolve().parents[1]\n"
            "    out = root / 'models' / 'qwen-image-2.1-unc'\n"
            "    out.mkdir(parents=True, exist_ok=True)\n"
            "    for f in FILES:\n"
            "        print(f'>>> {f}', flush=True)\n"
            "        p = hf_hub_download(repo_id=REPO, filename=f, local_dir=out)\n"
            "        print(f'    done: {os.path.getsize(p)/1024**3:.2f} GB', flush=True)\n"
            "    print('All files ready. Start the app with start.bat')\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    main()\n",
            encoding="utf-8",
        )

    _write(STAGE / ".env.example", 
        "# QwenImages Portable configuration\r\n"
        "# mock = placeholder images (no GPU work) | local = diffusers | comfy = ComfyUI engine\r\n"
        "ENGINE_MODE=comfy\r\n"
        "\r\n"
        "HOST=127.0.0.1\r\n"
        "PORT=8000\r\n"
        "\r\n"
        "# Engine transport: inproc = in the same process (fastest, default)\r\n"
        "#                   http   = connect to a standalone engine at ENGINE_URL\r\n"
        "# Single-app mode (run_app.bat) listens on APP_PORT\r\n"
        "ENGINE_TRANSPORT=inproc\r\n"
        "ENGINE_URL=http://127.0.0.1:8189\r\n"
        "APP_PORT=8000\r\n"
        "\r\n"
        "# ComfyUI engine\r\n"
        "COMFYUI_URL=http://127.0.0.1:8188\r\n"
        "COMFYUI_AUTOSTART=true\r\n"
        "COMFYUI_DIR=comfyui\r\n"
        "COMFYUI_PORT=8188\r\n"
        "# 16GB+ : leave empty for full speed. 6GB cards: set to --lowvram\r\n"
        "COMFYUI_EXTRA_ARGS=\r\n"
        "\r\n"
        "# Models (under models/qwen-image-2.1-unc)\r\n"
        "MODEL_DIR=models/qwen-image-2.1-unc\r\n"
        "TRANSFORMER_FILE=qwen-image-2.1-UC-Q4_K_M.gguf\r\n"
        "TEXT_ENCODER_FILE=text_encoders/qwen3vl_8b_int8_convrot.safetensors\r\n"
        "VAE_FILE=vae/qwen_image_2.1_vae_bf16.safetensors\r\n"
        "\r\n"
        "# Generation defaults (per-project \"profile\" in the UI overrides these)\r\n"
        "# 16GB target profile: 1024 / 24 steps. 6GB dev profile: 768 / 8 steps\r\n"
        "DEFAULT_WIDTH=1024\r\n"
        "DEFAULT_HEIGHT=1024\r\n"
        "DEFAULT_STEPS=24\r\n"
        "STORYBOARD_STEPS=24\r\n"
        "# 0 = use the model's own sigma schedule (Qwen-Image 2.1 ships shift=0.69).\r\n"
        "# The old 3.1 from the 1.0/2509 template dilutes the reference condition.\r\n"
        "SAMPLING_SHIFT=0\r\n"
        "DEFAULT_GUIDANCE=4.0\r\n"
        "MAX_BATCH=4\r\n"
        "MAX_PIXELS=2361960\r\n"
        "INFER_TIMEOUT_SECONDS=3600\r\n"
        "MAX_QUEUE_SIZE=20\r\n"
        "\r\n"
        "# Local text model (comic studio: script / character / scene extraction)\r\n"
        "# Reuses the Qwen3-VL-8B text encoder shipped with Qwen-Image, no extra download\r\n"
        "LLM_ENABLED=true\r\n"
        "LLM_MAX_LENGTH=768\r\n"
        "LLM_TIMEOUT_SECONDS=1800\r\n"
        "\r\n"
        "# External tools\r\n"
        "# ffmpeg: leave empty to use runtime/ffmpeg (bundled) or system PATH\r\n"
        "FFMPEG_PATH=\r\n"
        "\r\n"
        "# VRAM slots: image gen and narration share one GPU - whoever is needed\r\n"
        "# gets loaded, the other is evicted. After SLOT_IDLE_UNLOAD_SECONDS idle,\r\n"
        "# everything is unloaded to free VRAM.\r\n"
        "MODEL_SLOTS_ENABLED=true\r\n"
        "SLOT_IDLE_UNLOAD_SECONDS=600\r\n"
        "\r\n"
        "# Narration. edge-tts works out of the box (no model, no VRAM, needs\r\n"
        "# internet). IndexTTS-2 is optional and much heavier - see install_tts.bat\r\n"
        "TTS_ENABLED=true\r\n"
        "TTS_URL=http://127.0.0.1:8190\r\n"
        "TTS_BACKEND=auto\r\n"
        "TTS_VOICE=zh-CN-XiaoxiaoNeural\r\n"
        "TTS_HOME=tools/index-tts\r\n"
        "TTS_PYTHON=\r\n"
        "TTS_AUTOSTART=true\r\n"
        "TTS_MODELS_DIR=checkpoints_2\r\n"
        "TTS_MODEL_VERSION=2\r\n"
        "TTS_USE_FP16=true\r\n"
        "TTS_SPEAKER=\r\n"
        "TTS_TIMEOUT_SECONDS=300\r\n"
        "\r\n"
        "DEVICE=cuda:0\r\n"
        "DTYPE=float16\r\n"
        "KEEP_LOADED=true\r\n"
        "IDLE_UNLOAD_SECONDS=1800\r\n"
        "VAE_TILING=true\r\n"
        "LOW_VRAM=false\r\n"
        "\r\n"
        "DATA_DIR=data\r\n"
        "OUTPUT_DIR=data/outputs\r\n"
        "DB_PATH=data/app.db\r\n",
        encoding="utf-8",
    )

    _write(STAGE / "README-便携版.txt", 
        """QwenImages 本地图片生成器 · 绿色便携版
========================================

一、这是什么
  内置 Python 运行时和全部依赖，解压即可运行，无需安装任何东西。
  模型权重（约 14.6 GB）未包含在包内，首次使用需下载一次。

二、快速开始
  1. 双击 download_models.bat 下载模型（约 14.6 GB，走国内镜像，支持断点续传，
     中断后重新运行即可续传）
  2. 双击 start.bat 启动
  3. 浏览器打开 http://127.0.0.1:8000

  启动后服务会自动拉起 ComfyUI（约 45 秒）并按需加载模型，无需手动操作。

三、目录说明
  runtime/python/   内置 Python 3.13 + torch(CUDA) + 全部依赖，请勿单独搬运
  runtime/ffmpeg/   自带 ffmpeg（成片渲染用），无需安装
  models/           模型权重（由 download_models.bat 填充）
  data/             生成的图片与数据库（你的数据，可整目录备份）
  .env              配置文件（首次启动自动从 .env.example 生成）

  portal/           平台外壳：卡片墙、按 manifest 自动发现并挂载应用
  apps/             业务应用（各自自包含：manifest + backend + frontend）
                      comic      漫画工作室（剧本 → 分镜 → 出图 → 拼页）
                      imagegen   单张出图
                      portrait   人像 / 换装
                      dango      团子衣橱（Q 版捏人 + 衣柜 + 试衣间）
                      storyboard 团子小剧场（一句话 → 分镜 → 旁白 → 竖屏成片）
  engine/           模型能力层：ComfyUI 通道、任务调度、卡死恢复、显存槽位
  tools/            旁白服务（tts_worker）；index-tts 由 install_tts.bat 按需拉取

  启动方式（都在根目录）：
    start.bat 或 run_platform.bat   门户一体启动（推荐，五个应用共用引擎）
    run_app.bat                     只起某一个应用（独立端口，省显存）
    run_engine.bat                  引擎单独跑一个进程，门户用 ENGINE_TRANSPORT=http 连
    run_tts.bat                     手动启动旁白服务（默认 edge-tts，开箱即用）
    install_tts.bat                 可选：装本地高清旁白 IndexTTS-2（约 5.5 GB）
    check.bat                       自检：这份代码的参考图能否真进模型（5 秒，不用显卡）

四、显存与旁白
  旁白默认走 edge-tts（微软在线合成）：不下载任何模型、不占显存、秒级出声，
  需要联网。音色在界面里选（晓晓 / 晓伊 / 云希 …），换行即换人。
  这种模式下出图与旁白互不干扰，不存在抢显存的问题。

  想要完全离线或更高音质，再双击 install_tts.bat 装 IndexTTS-2（5.5 GB，
  独立 Python 3.11 环境）。装好之后 auto 会自动改用它，此时它要占显存：
  出图与旁白用哪个就装哪个，另一个当场卸载；两者都空闲超过
  SLOT_IDLE_UNLOAD_SECONDS（默认 600 秒）自动全部卸载，把显存还给系统。
  都没装时旁白退成占位音，只用于验证音画对齐。

五、硬件要求
  NVIDIA 显卡。本包默认按 16GB 显存调优（1024 / 24 步）：
  界面里新建项目时选「生成档位」即可切换：
    · 目标档 · 16GB 画质（1024×1024 / 24 步）—— 画质与画风还原用这档测
    · 开发档 · 6GB 友好（768×768 / 8 步）—— 小显存机器选这档，
      并把 .env 里 COMFYUI_EXTRA_ARGS 改回 --lowvram
  文本编码器的设备由 ComfyUI 自己调度，无需在 .env 里配。
  画风库 / 主题色数据来自 yang0/handraw-style（MIT）。

六、常见问题
  * 提示端口占用：关闭已开实例，或改 .env 中 PORT
  * 想换画质档：改 .env 中 TRANSFORMER_FILE 为其他量化文件并重新下载对应权重
  * 移动整个包：直接拷贝整个文件夹即可，无注册表、无全局依赖
  * 卸载：删除文件夹即可，干净无残留

七、合规提示
  本包使用的模型基于 Qwen Research License，输出内容无内置安全过滤，
  生成内容的合法合规性由使用者自行负责。
""",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
