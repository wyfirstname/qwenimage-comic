# -*- coding: utf-8 -*-
"""旁白能力（IndexTTS-2）的引擎侧门面。

对接的是 `tools/tts_worker/server.py` 那个独立进程，不是直接 import indextts。
原因见 server.py 顶部：依赖版本互斥，只能跨进程。

这个模块负责四件事：
1. **找到能用的解释器**——必须是 index-tts 自己的 venv（3.10/3.11），不是主环境
2. **惰性拉起进程**——第一次要念旁白时才启动，不占开机时间
3. **抢槽位**——念之前先让出图模型把显存腾出来（engine/slots.py）
4. **说人话的失败信息**——「没装」和「装了但权重没下」是两件事，
   用户需要知道下一步该敲哪条命令

一切失败都以 TtsError 抛出，消息直接可以展示在界面上。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from shared.config import settings

# 进程句柄与实例分离：实例被丢弃后仍要能杀掉子进程（comfy_engine 的老教训）
_PROC: Optional[subprocess.Popen] = None
_LOCK = threading.RLock()
_SLOT_READY = False


class TtsError(RuntimeError):
    """旁白失败。消息面向用户，自带下一步动作。"""


# ------------------------------------------------------------
#  路径解析
# ------------------------------------------------------------

def worker_script() -> Path:
    return settings.project_root / "tools" / "tts_worker" / "server.py"


def _python_exe() -> Optional[Path]:
    """TTS 解释器：显式配置 > index-tts 自己的 .venv > 主程序解释器。

    最后一级回退是刻意的：server.py 只用标准库，任何 Python ≥3.9 都能跑。
    没有 IndexTTS 环境时用主解释器起 stub 后端，整条旁白链路（对齐、拼接、
    音画同步）依然可验证可用，界面上会如实标注「占位音」。
    """
    raw = (settings.tts_python or "").strip()
    if raw:
        p = Path(raw)
        if not p.is_absolute():
            p = (settings.project_root / p).resolve()
        return p if p.exists() else None

    home = settings.tts_home_path
    for n in ("Scripts/python.exe", "bin/python", "python.exe"):
        p = home / ".venv" / n
        if p.exists():
            return p
    return Path(sys.executable)


def stub_expected() -> bool:
    """没装 IndexTTS（也没配置独立解释器）时，拉起来的一定是占位后端。"""
    raw = (settings.tts_python or "").strip()
    if raw:
        return False
    home = settings.tts_home_path
    return not (home / ".venv" / "Scripts/python.exe").exists() \
        and not (home / ".venv" / "bin/python").exists()


def install_hint() -> str:
    """一句能直接照抄的安装提示。"""
    return (
        "旁白需要单独装 IndexTTS（它要求 Python 3.10/3.11，和主环境冲突，不能合并）。"
        "在项目根目录双击 install_tts.bat，或手动执行：\n"
        "  git clone https://github.com/index-tts/index-tts.git tools/index-tts\n"
        "  cd tools/index-tts && uv sync --all-extras\n"
        "  hf download IndexTeam/IndexTTS-2 --local-dir=checkpoints_2\n"
        "然后在 .env 里设置 TTS_HOME=tools/index-tts（默认值就是它，通常不用改）。"
    )


def _base_url() -> str:
    return (settings.tts_url or "http://127.0.0.1:8190").rstrip("/")


def _req(path: str, *, payload: Optional[dict] = None,
         timeout: float = 10.0) -> dict:
    url = f"{_base_url()}{path}"
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "ignore")
    try:
        return json.loads(body or "{}")
    except Exception:
        return {"ok": False, "error": body[:200]}


# ------------------------------------------------------------
#  进程与健康
# ------------------------------------------------------------

def _health(timeout: float = 2.0) -> Optional[dict]:
    try:
        d = _req("/health", timeout=timeout)
        return d if d.get("ok") else None
    except Exception:
        return None


def _log_path() -> Path:
    return settings.data_dir_path / "tts_worker.log"


def _spawn() -> None:
    global _PROC
    py = _python_exe()
    if py is None:
        raise TtsError(f"没有找到 IndexTTS 的解释器。\n{install_hint()}")
    script = worker_script()
    if not script.exists():
        raise TtsError(f"缺少旁白服务脚本：{script}（工程文件不完整）")

    home = settings.tts_home_path
    cmd = [
        str(py), str(script),
        "--port", str(_port()),
        "--home", str(home),
        "--models", settings.tts_models_dir,
        "--version", settings.tts_model_version,
    ]
    if not settings.tts_use_fp16:
        cmd.append("--no-fp16")

    settings.data_dir_path.mkdir(parents=True, exist_ok=True)
    log = open(_log_path(), "ab")  # noqa: SIM115 （随进程生命周期）
    flags = 0
    if os.name == "nt":
        # 不要弹出黑色控制台窗口——用户会以为程序崩了
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    _PROC = subprocess.Popen(cmd, cwd=str(home if home.exists() else settings.project_root),
                             stdout=log, stderr=subprocess.STDOUT,
                             creationflags=flags, env=env)
    print(f"[tts] 已拉起旁白进程 PID={_PROC.pid}，日志 {_log_path()}", flush=True)


def _port() -> int:
    try:
        return int(_base_url().rsplit(":", 1)[1].split("/")[0])
    except Exception:
        return 8190


def _log_tail(n: int = 8) -> str:
    try:
        lines = _log_path().read_text(encoding="utf-8", errors="ignore").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def _ensure_worker(*, wait: float = 45.0) -> dict:
    """确保旁白进程活着。返回 /health。"""
    h = _health()
    if h:
        return h
    if not settings.tts_autostart:
        raise TtsError(
            f"旁白服务没有在运行（{_base_url()}），且 TTS_AUTOSTART 已关闭。\n"
            "手工启动：双击 run_tts.bat"
        )
    with _LOCK:
        h = _health()
        if h:
            return h
        if _PROC is not None and _PROC.poll() is None:
            pass  # 还活着，只是还没起好
        else:
            _spawn()

    deadline = time.time() + wait
    while time.time() < deadline:
        h = _health()
        if h:
            return h
        if _PROC is not None and _PROC.poll() is not None:
            tail = _log_tail()
            raise TtsError(
                "旁白进程启动后立即退出，日志尾部：\n" + (tail or "（空）")
                + "\n\n最常见的原因是解释器不对（必须是 index-tts 的 .venv）"
                  "或权重没下载完整。"
            )
        time.sleep(0.4)
    raise TtsError(
        f"旁白进程 {wait:.0f} 秒内没有就绪。日志：{_log_path()}\n" + _log_tail()
    )


def _register_slot() -> None:
    """把旁白挂进显存槽位表（只挂一次）。"""
    global _SLOT_READY
    if _SLOT_READY:
        return
    from engine.slots import slots

    slots.register("tts", unload=unload, is_loaded=is_loaded)
    _SLOT_READY = True


# ------------------------------------------------------------
#  对外能力
# ------------------------------------------------------------

def available() -> tuple[bool, str]:
    """不启动进程，只判断「理论上能不能用」。给界面提示用。

    第二个返回值是提示而非错误：装了 stub 也能用，但要告诉用户那是占位音。
    """
    if not settings.tts_enabled:
        return False, "旁白功能已在配置中关闭（TTS_ENABLED=false）"
    if not worker_script().exists():
        return False, "缺少 tools/tts_worker/server.py，工程文件不完整"
    if _health(timeout=1.0):
        return True, ""
    if _python_exe() is not None:
        if stub_expected():
            return True, ("未安装 IndexTTS，将使用占位音验证链路。"
                          f"{install_hint()}")
        return True, ""
    return False, install_hint()


def is_loaded() -> bool:
    h = _health(timeout=1.5)
    return bool(h and h.get("loaded"))


def status() -> dict:
    h = _health(timeout=1.5)
    py = _python_exe()
    out = {
        "enabled": bool(settings.tts_enabled),
        "url": _base_url(),
        "running": bool(h),
        "ready": bool(h and h.get("loaded")),
        "backend": (h or {}).get("backend", ""),
        "stub_expected": stub_expected(),
        "python": str(py) if py else "",
        "home": str(settings.tts_home_path),
        "models": settings.tts_models_dir,
        "version": settings.tts_model_version,
        "pid": (h or {}).get("pid", 0),
    }
    if not h:
        ok, reason = available()
        out["reason"] = reason
    else:
        try:
            out.update({"stats": _req("/status", timeout=2.0).get("stats", {})})
        except Exception:
            pass
    return out


def load() -> dict:
    """把权重装进显存（会先让出图模型让位）。"""
    _register_slot()
    from engine.slots import slots

    slots.acquire("tts")
    _ensure_worker()
    r = _req("/load", payload={}, timeout=float(settings.tts_timeout_seconds))
    if not r.get("ok"):
        raise TtsError(f"加载旁白模型失败：{r.get('error') or r}")
    return r


def unload() -> None:
    global _PROC
    h = _health(timeout=1.0)
    if h:
        try:
            _req("/unload", payload={}, timeout=20)
        except Exception:
            pass
    # 进程本身不杀：下次要念时省掉一遍解释器启动 + CUDA 初始化（好几秒）
    if _PROC is not None and _PROC.poll() is not None:
        _PROC = None


def shutdown() -> None:
    """彻底停掉旁白进程。平台退出或用户主动回收显存时用。"""
    global _PROC
    h = _health(timeout=1.0)
    if h:
        try:
            _req("/shutdown", payload={}, timeout=5)
        except Exception:
            pass
    if _PROC is not None:
        for _ in range(25):
            if _PROC.poll() is not None:
                break
            time.sleep(0.2)
        if _PROC.poll() is None:
            try:
                _PROC.kill()
            except Exception:
                pass
        _PROC = None


def _wait_image_idle(max_wait: float = 180.0) -> None:
    """等出图队列空下来再抢显存。

    抢槽位 = 卸载对方的权重。如果此刻 ComfyUI 正在跑一张图，卸载请求会把它
    的模型从显存里抽走，正在执行的推理直接失败、那张图白等几分钟。所以宁可
    让旁白等一会儿——念一句话本来就是几秒钟的事，等得起。
    """
    try:
        from engine.infer import scheduler
    except Exception:
        return
    deadline = time.time() + max_wait
    announced = False
    while time.time() < deadline:
        try:
            if scheduler.running_job() is None:
                return
        except Exception:
            return
        if not announced:
            print("[tts] 出图任务正在跑，等它结束再让出显存…", flush=True)
            announced = True
        time.sleep(1.0)


def synth(text: str, out_path: Path | str, *, speaker: str = "",
          emo_wav: str = "", emo_alpha: Optional[float] = None,
          emo_text: str = "", lang: str = "", speed: float = 0.0) -> dict:
    """把一句中文念成 wav。返回 {path, duration, backend, seconds}。"""
    text = (text or "").strip()
    if not text:
        raise TtsError("旁白文本为空")

    _register_slot()
    from engine.slots import slots

    _wait_image_idle()            # 别把正在出的图打断
    slots.acquire("tts")          # 让出图模型把显存让出来
    _ensure_worker()

    payload = {
        "text": text,
        "out_path": str(out_path),
        "speaker": speaker or settings.tts_speaker,
    }
    if emo_wav:
        payload["emo_wav"] = emo_wav
    if emo_alpha is not None:
        payload["emo_alpha"] = float(emo_alpha)
    if emo_text:
        payload["emo_text"] = emo_text
    if lang and str(settings.tts_model_version).startswith("2.5"):
        payload["lang"] = lang
    if speed and str(settings.tts_model_version).startswith("2.5"):
        payload["speed"] = float(speed)

    try:
        r = _req("/synth", payload=payload,
                 timeout=float(settings.tts_timeout_seconds))
    except urllib.error.URLError as exc:
        raise TtsError(f"连接旁白服务失败：{exc}\n看看 {_log_path()}") from exc
    if not r.get("ok"):
        raise TtsError(f"旁白合成失败：{r.get('error') or r}")

    slots.touch("tts")
    return r


__all__ = ["TtsError", "available", "status", "synth", "load", "unload",
           "shutdown", "is_loaded", "install_hint", "worker_script"]
