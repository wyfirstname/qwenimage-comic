# -*- coding: utf-8 -*-
"""IndexTTS 旁白服务（独立进程，只依赖标准库 + IndexTTS 自身）。

为什么是独立进程
----------------
IndexTTS-2 的依赖是 `python>=3.10,<3.12` + `torch 2.8` + `transformers 4.52.1`，
而主工程跑在 Python 3.13 / torch 2.14 / transformers 5.x（ComfyUI 的 GGUF 通道
要这个版本）。两边硬塞进一个 venv，结果一定是同归于尽。

所以职责这样切：
    * 主工程  → 出图、分镜、渲染，调本服务的 HTTP 接口
    * 本服务  → 只干一件事：把一段中文念成 wav

顺带白拿一个好处：模型权重在**另一个进程**里，卸载就能真把显存还回去
（`torch.cuda.empty_cache()` 之后显存归零）。同进程里"卸载"永远只是自欺欺人。

为什么不用 FastAPI
------------------
这个文件要在 IndexTTS 的 venv 里跑。那个环境是 uv 按它自己的 pyproject 建的，
多装一个 web 框架就多一份依赖冲突的可能。标准库的 http.server 足够——
一个本机回环、串行推理、每分钟几次请求的服务，不需要异步框架。

用法
----
    <index-tts>/.venv/Scripts/python.exe server.py --port 8190 \
        --home <index-tts 目录> --models checkpoints_2 --version 2 --fp16

接口（全部 JSON，仅监听 127.0.0.1）
----------------------------------
    GET  /health   存活 + 是否已加载
    GET  /status   详情（后端、显存、权重路径）
    POST /load     加载权重（阻塞到就绪）
    POST /unload   卸载权重、释放显存
    POST /synth    {text, out_path, speaker, emo_wav, emo_alpha, lang, speed}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------- 全局状态
_LOCK = threading.Lock()
_BACKEND = None            # 惰性创建
_CFG: dict = {}
_STATS = {"synth_count": 0, "last_error": "", "last_seconds": 0.0,
          "loaded_at": 0.0, "load_seconds": 0.0}


# ================================================================
#  后端：真实 IndexTTS
# ================================================================
class IndexBackend:
    """IndexTTS-2 / 2.5 的薄封装。权重只在 load() 时才进显存。"""

    name = "indextts"

    def __init__(self, home: Path, models: str, version: str, fp16: bool):
        self.home = home
        self.models = models
        self.version = str(version)
        self.fp16 = fp16
        self.tts = None
        self.model_dir = (home / models).resolve()

    # ---------------- 加载 ----------------
    def load(self) -> None:
        if self.tts is not None:
            return
        if not self.model_dir.exists():
            raise RuntimeError(
                f"权重目录不存在：{self.model_dir}\n"
                "下载方式（在 index-tts 目录下）：\n"
                "  uv tool install huggingface-hub\n"
                f"  hf download IndexTeam/IndexTTS-{self.version} "
                f"--local-dir={self.models}"
            )
        cfg = self.model_dir / "config.yaml"
        if not cfg.exists():
            raise RuntimeError(f"缺少配置文件：{cfg}（权重没下完整）")

        # 让 `import indextts` 能找到包——服务是从任意 cwd 被拉起来的
        if str(self.home) not in sys.path:
            sys.path.insert(0, str(self.home))

        started = time.time()
        if self.version.startswith("2.5"):
            from indextts.infer_v2_5 import IndexTTS2  # type: ignore

            self.tts = IndexTTS2(cfg_path=str(cfg), model_dir=str(self.model_dir),
                                 use_bf16=bool(self.fp16))
        else:
            from indextts.infer_v2 import IndexTTS2  # type: ignore

            self.tts = IndexTTS2(cfg_path=str(cfg), model_dir=str(self.model_dir),
                                 use_fp16=bool(self.fp16),
                                 use_cuda_kernel=False, use_deepspeed=False)
        _STATS["load_seconds"] = round(time.time() - started, 2)
        _STATS["loaded_at"] = time.time()

    # ---------------- 卸载 ----------------
    def unload(self) -> None:
        if self.tts is None:
            return
        self.tts = None
        import gc

        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
        except Exception:
            pass
        _STATS["loaded_at"] = 0.0

    @property
    def loaded(self) -> bool:
        return self.tts is not None

    # ---------------- 合成 ----------------
    def synth(self, req: dict) -> dict:
        self.load()
        out = Path(req["out_path"])
        out.parent.mkdir(parents=True, exist_ok=True)

        kwargs: dict = {
            "spk_audio_prompt": req["speaker"],
            "text": req["text"],
            "output_path": str(out),
            "verbose": False,
        }
        # 2.5 才有 lang / duration_factor；2.0 传了会 TypeError
        if self.version.startswith("2.5"):
            if req.get("lang"):
                kwargs["lang"] = req["lang"]
            if req.get("speed"):
                kwargs["duration_factor"] = 1.0 / float(req["speed"])
        if req.get("emo_wav"):
            kwargs["emo_audio_prompt"] = req["emo_wav"]
            if req.get("emo_alpha") is not None:
                kwargs["emo_alpha"] = float(req["emo_alpha"])
        if req.get("emo_text"):
            kwargs["use_emo_text"] = True
            kwargs["emo_text"] = req["emo_text"]
            if req.get("emo_alpha") is not None:
                kwargs["emo_alpha"] = float(req["emo_alpha"])

        self.tts.infer(**kwargs)
        if not out.exists() or out.stat().st_size < 1024:
            raise RuntimeError(f"合成没有产出有效音频：{out}")
        return {"path": str(out), "bytes": out.stat().st_size}

    def info(self) -> dict:
        vram = {}
        try:
            import torch

            if torch.cuda.is_available():
                vram = {
                    "allocated_mb": round(torch.cuda.memory_allocated() / 1024 ** 2),
                    "reserved_mb": round(torch.cuda.memory_reserved() / 1024 ** 2),
                    "device": torch.cuda.get_device_name(0),
                }
        except Exception:
            pass
        return {"version": self.version, "model_dir": str(self.model_dir),
                "fp16": self.fp16, "vram": vram}

    def resolve_speaker(self, raw: str) -> str:
        return _resolve_speaker(raw)


# ================================================================
#  后端：edge-tts（零模型、零显存，开箱即用）
# ================================================================
#  zh-CN 常用音色。给界面做下拉用，也是合法值校验的白名单。
EDGE_VOICES = {
    "zh-CN-XiaoxiaoNeural": "晓晓 · 女声 · 温柔（默认）",
    "zh-CN-XiaoyiNeural": "晓伊 · 女声 · 活泼",
    "zh-CN-YunxiNeural": "云希 · 男声 · 青年",
    "zh-CN-YunjianNeural": "云健 · 男声 · 沉稳",
    "zh-CN-YunyangNeural": "云扬 · 男声 · 播报",
    "zh-CN-liaoning-XiaobeiNeural": "晓北 · 女声 · 东北",
    "zh-CN-shaanxi-XiaoniNeural": "晓妮 · 女声 · 陕西",
}
EDGE_DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"


def _find_ffmpeg() -> str:
    """找 ffmpeg。刻意不 import shared.ffmpeg——本文件要能在任何解释器里跑。

    顺序与 shared/ffmpeg.py 一致：环境变量 > 包内 runtime > PATH。
    """
    import shutil

    raw = (os.environ.get("FFMPEG_PATH") or "").strip().strip('"')
    if raw and Path(raw).exists():
        return raw
    root = Path(__file__).resolve().parents[2]
    for rel in ("runtime/ffmpeg/bin/ffmpeg.exe", "runtime/ffmpeg/ffmpeg.exe",
                "runtime/ffmpeg/bin/ffmpeg", "tools/ffmpeg/bin/ffmpeg.exe"):
        p = root / rel
        if p.exists():
            return str(p)
    return shutil.which("ffmpeg") or "ffmpeg"


class EdgeBackend:
    """微软 edge-tts：文字直接走在线接口出语音，本地不落任何权重。

    为什么把它放在第一顺位：零下载、零显存、秒级出声，音质比 82M 小模型好，
    而且和出图完全不抢显卡。缺点是必须联网；断网时自动降级到系统语音或占位音。
    """

    name = "edge"

    def __init__(self, voice: str = EDGE_DEFAULT_VOICE, rate: str = ""):
        self.voice = voice if voice in EDGE_VOICES else EDGE_DEFAULT_VOICE
        self.rate = rate
        self._loaded = False

    # 无权重可载，load/unload 只是标记状态，保持接口一致
    def load(self) -> None:
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    @property
    def loaded(self) -> bool:
        return self._loaded

    def resolve_speaker(self, raw: str) -> str:
        """edge 的 speaker 是音色名，不是参考音频路径。"""
        raw = (raw or "").strip()
        return raw if raw in EDGE_VOICES else self.voice

    def synth(self, req: dict) -> dict:
        try:
            import edge_tts
        except ImportError as exc:
            raise RuntimeError(
                "当前解释器没有安装 edge-tts。装法：pip install edge-tts"
            ) from exc

        out = Path(req["out_path"])
        out.parent.mkdir(parents=True, exist_ok=True)
        voice = self.resolve_speaker(req.get("speaker") or "")
        # 语速：storyboard 传 speed=1.2 → edge 的 "+20%"
        rate = self.rate
        speed = req.get("speed")
        if speed:
            pct = round((float(speed) - 1.0) * 100)
            if abs(pct) >= 1:
                rate = f"{pct:+d}%"

        mp3 = out.with_suffix(".edge.mp3")
        kwargs = {"voice": voice}
        if rate:
            kwargs["rate"] = rate

        async def _run() -> None:
            comm = edge_tts.Communicate(str(req["text"]), **kwargs)
            await comm.save(str(mp3))

        try:
            asyncio.run(_run())
        except Exception as exc:
            raise RuntimeError(
                f"edge-tts 合成失败（该通道需要联网）：{type(exc).__name__}: {exc}"
            ) from exc

        if not mp3.exists() or mp3.stat().st_size < 512:
            raise RuntimeError("edge-tts 没有产出音频，通常是网络被拦")

        # 统一成 24k 单声道 wav：下游时长探测、拼接、混流都按 wav 处理
        ff = _find_ffmpeg()
        proc = subprocess.run(
            [ff, "-y", "-v", "error", "-i", str(mp3),
             "-ar", "24000", "-ac", "1", str(out)],
            capture_output=True, text=True,
        )
        mp3.unlink(missing_ok=True)
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError(f"ffmpeg 转码失败：{proc.stderr[-300:]}")

        self._loaded = True
        return {"path": str(out), "bytes": out.stat().st_size,
                "voice": voice, "note": "edge-tts 在线合成"}

    def info(self) -> dict:
        return {"version": "edge-tts", "voice": self.voice,
                "voices": list(EDGE_VOICES), "vram": {}}


# ================================================================
#  后端：占位（没网、没装任何模型时的最后兜底）
# ================================================================
class StubBackend:
    """不加载任何模型，按字数估时长，生成一段柔和的正弦音。

    存在的意义：链路验证不该被 5.5GB 权重和 CUDA 卡住。有了它，
    旁白对齐、音轨拼接、字幕时间轴这些逻辑在任何机器上都能测。
    它生成的音频**是有声音的**（不是静音），这样才能证明混流真的生效。
    """

    name = "stub"

    def __init__(self, reason: str):
        self.reason = reason
        self._loaded = False

    def load(self) -> None:
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    @property
    def loaded(self) -> bool:
        return self._loaded

    def synth(self, req: dict) -> dict:
        self._loaded = True
        out = Path(req["out_path"])
        out.parent.mkdir(parents=True, exist_ok=True)
        text = str(req.get("text") or "")
        # 中文旁白约 4.5 字/秒，夹一点随机感，别每句都一样长
        seconds = max(0.8, len(text) / 4.5)
        self._write_wav(out, seconds=seconds)
        return {"path": str(out), "bytes": out.stat().st_size,
                "note": f"stub 占位音频（{self.reason}）"}

    @staticmethod
    def _write_wav(path: Path, *, seconds: float, rate: int = 22050) -> None:
        import array
        import math

        n = int(seconds * rate)
        buf = array.array("h")
        # 低频颤音 + 淡入淡出包络：听起来像一句含糊的哼唱，方便确认时间轴
        for i in range(n):
            t = i / rate
            env = min(1.0, t / 0.12, (n - i) / (rate * 0.18))
            vib = math.sin(2 * math.pi * 220 * t) * 0.6
            vib += math.sin(2 * math.pi * 330 * t + 0.7) * 0.25
            buf.append(int(max(-1.0, min(1.0, vib * env)) * 9000))
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(buf.tobytes())

    def info(self) -> dict:
        return {"version": "stub", "reason": self.reason}

    def resolve_speaker(self, raw: str) -> str:
        return (raw or "").strip()


# ================================================================
#  后端选择
# ================================================================
def _try_index(cfg: dict):
    """能装 IndexTTS 就返回 (后端, "")，否则 (None, 原因)。"""
    home = Path(cfg["home"]).resolve()
    if not home.exists():
        return None, f"未找到 index-tts 目录：{home}"
    # 半途中断的 clone 只留下 .git，直接跳过，别去 import 一个不存在的包
    if not (home / "indextts").is_dir():
        return None, f"index-tts 目录不完整：{home}"
    if str(home) not in sys.path:
        sys.path.insert(0, str(home))
    try:
        import importlib.util

        mod = "indextts.infer_v2_5" if str(cfg["version"]).startswith("2.5") else "indextts.infer_v2"
        if importlib.util.find_spec("indextts") is None:
            return None, "当前解释器里没有安装 indextts"
        importlib.import_module(mod)
    except Exception as exc:
        return None, f"导入 indextts 失败：{type(exc).__name__}: {exc}"
    return IndexBackend(home, cfg["models"], cfg["version"], cfg["fp16"]), ""


def build_backend(cfg: dict):
    want = str(cfg.get("backend") or "auto").lower()
    if cfg.get("stub") or want == "stub":
        return StubBackend("显式指定 stub")

    reasons: list[str] = []

    # auto 的顺序：装了 IndexTTS 就用它——那是用户主动跑过 install_tts.bat，
    # 意图明确，不能白白让 5.5GB 权重躺着。没装才轮到 edge-tts。
    if want in ("auto", "indextts"):
        b, why = _try_index(cfg)
        if b is not None:
            return b
        reasons.append(why)
        if want == "indextts":
            return StubBackend(why)

    if want in ("auto", "edge"):
        try:
            import edge_tts  # noqa: F401

            return EdgeBackend(cfg.get("voice") or EDGE_DEFAULT_VOICE,
                               cfg.get("rate") or "")
        except Exception as exc:
            reasons.append(f"edge-tts 不可用：{type(exc).__name__}: {exc}")
            if want == "edge":
                return StubBackend(reasons[-1])

    return StubBackend("；".join(reasons) or "没有可用的旁白后端")


def backend():
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = build_backend(_CFG)
    return _BACKEND


# ================================================================
#  HTTP
# ================================================================
def _resolve_speaker(raw: str) -> str:
    """音色参考音频：绝对路径直用；相对路径按 index-tts 目录解析。"""
    if not raw:
        home = Path(_CFG["home"]).resolve()
        # 内置示例音色，首次运行会自动下载
        cand = home / "examples" / "voice_01.wav"
        return str(cand)
    p = Path(raw)
    if p.is_absolute():
        return str(p)
    return str((Path(_CFG["home"]).resolve() / raw).resolve())


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # 关掉默认的 stderr 访问日志——客户端会轮询 /health，会把日志刷满
    def log_message(self, fmt, *args):  # noqa: A003
        pass

    # ---------------- 工具 ----------------
    def _send(self, code: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---------------- 路由 ----------------
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/health"):
            b = backend()
            self._send(200, {"ok": True, "backend": b.name, "loaded": b.loaded,
                             "pid": os.getpid()})
        elif self.path.startswith("/status"):
            b = backend()
            self._send(200, {"ok": True, "backend": b.name, "loaded": b.loaded,
                             "pid": os.getpid(), "stats": _STATS, "info": b.info()})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        req = self._body()
        if path == "/shutdown":
            self._send(200, {"ok": True})
            threading.Thread(target=_die, daemon=True).start()
            return
        try:
            with _LOCK:                      # 串行：显存里只有一份权重
                if path == "/load":
                    b = backend()
                    b.load()
                    self._send(200, {"ok": True, "backend": b.name,
                                     "load_seconds": _STATS["load_seconds"]})
                elif path == "/unload":
                    b = backend()
                    b.unload()
                    self._send(200, {"ok": True, "backend": b.name, "loaded": b.loaded})
                elif path == "/synth":
                    self._synth(req)
                else:
                    self._send(404, {"ok": False, "error": "not found"})
        except Exception as exc:
            _STATS["last_error"] = f"{type(exc).__name__}: {exc}"
            tb = traceback.format_exc(limit=4)
            self._send(500, {"ok": False, "error": str(exc), "trace": tb})

    def _synth(self, req: dict) -> None:
        text = str(req.get("text") or "").strip()
        if not text:
            self._send(400, {"ok": False, "error": "text 不能为空"})
            return
        out_path = str(req.get("out_path") or "").strip()
        if not out_path:
            self._send(400, {"ok": False, "error": "out_path 不能为空"})
            return
        req["text"] = text
        req["out_path"] = out_path
        started = time.time()
        b = backend()
        # speaker 的语义随后端变：index 是参考音频路径，edge 是音色名
        req["speaker"] = b.resolve_speaker(str(req.get("speaker") or ""))
        for k in ("emo_wav",):
            if req.get(k):
                req[k] = _resolve_speaker(str(req[k]))
        result = b.synth(req)
        seconds = round(time.time() - started, 2)
        _STATS["synth_count"] += 1
        _STATS["last_seconds"] = seconds
        dur = _wav_seconds(Path(result["path"]))
        self._send(200, {"ok": True, "backend": b.name, **result,
                         "duration": dur, "seconds": seconds,
                         "loaded": b.loaded})


def _wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate() or 1
            return round(w.getnframes() / rate, 3)
    except Exception:
        return 0.0


def _die() -> None:
    time.sleep(0.2)
    os._exit(0)


# ================================================================
#  入口
# ================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="Narration worker (edge-tts / IndexTTS / stub)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8190)
    ap.add_argument("--home", default=os.environ.get("TTS_HOME", "."))
    ap.add_argument("--models", default=os.environ.get("TTS_MODELS", "checkpoints_2"))
    ap.add_argument("--version", default=os.environ.get("TTS_VERSION", "2"))
    ap.add_argument("--backend", default=os.environ.get("TTS_BACKEND", "auto"),
                    choices=["auto", "edge", "indextts", "stub"],
                    help="auto=装了 IndexTTS 用它，否则 edge，都没有则占位音")
    ap.add_argument("--voice", default=os.environ.get("TTS_VOICE", EDGE_DEFAULT_VOICE),
                    help="edge-tts 音色名，如 zh-CN-XiaoxiaoNeural")
    ap.add_argument("--stub", action="store_true", help="不加载模型，生成占位音频")
    ap.add_argument("--no-fp16", action="store_true")
    args = ap.parse_args()

    _CFG.update({
        "home": args.home, "models": args.models, "version": args.version,
        "fp16": not args.no_fp16, "stub": args.stub,
        "backend": args.backend, "voice": args.voice,
    })

    b = backend()   # 只探测能装哪个后端，不加载权重
    print(f"[tts] backend={b.name} home={args.home} models={args.models} "
          f"version={args.version} fp16={_CFG['fp16']}", flush=True)
    if isinstance(b, EdgeBackend):
        print(f"[tts] edge-tts 音色={b.voice}（零模型，需联网）", flush=True)
    if isinstance(b, StubBackend):
        print(f"[tts] 占位模式：{b.reason}", flush=True)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"[tts] READY http://{args.host}:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
