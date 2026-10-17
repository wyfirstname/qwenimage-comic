# -*- coding: utf-8 -*-
"""ffmpeg 定位与调用（shared 层，纯公共）。

为什么要单独一层：ffmpeg 不是「分镜工坊」的私人物品——以后的视频引擎、
音频拼接、封面导出都要用它。把它钉在某个 apps/ 里，第二个应用就得复制一份
查找逻辑，然后两份逻辑慢慢长得不一样。

查找顺序（先近后远，先显式后隐式）
--------------------------------
1. 环境变量 `FFMPEG_PATH`（文件或目录都行）
2. 便携包自带 `<root>/runtime/ffmpeg/`
3. 开发机常见落点 `<root>/runtime/bin`、`<root>/tools/ffmpeg/bin`
4. 系统 PATH

结果做缓存：每次渲染都扫盘 + 起子进程探活，太浪费。
显式改配置后调用 `reset_cache()` 即可重新探测。

为什么要有 ffprobe
------------------
渲染前需要知道每段旁白的真实时长，才能把画面段落对齐到声音上。
`ffmpeg -i` 的 stderr 里也能抠出时长，但格式随版本变，不如 ffprobe 的 JSON 稳。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

from shared.config import settings

_PROBE_LOCK = threading.Lock()
_CACHE: dict[str, Optional[str]] = {"ffmpeg": None, "ffprobe": None}
_PROBED = {"ffmpeg": False, "ffprobe": False}
_FILTERS: dict[str, bool] = {}

_NAMES = ("ffmpeg.exe", "ffmpeg") if os.name == "nt" else ("ffmpeg",)
_PROBE_NAMES = ("ffprobe.exe", "ffprobe") if os.name == "nt" else ("ffprobe",)


def _search_dirs() -> list[Path]:
    root = settings.project_root
    return [
        root / "runtime" / "ffmpeg",          # 便携包（bin 与本目录两种放法都认）
        root / "runtime" / "ffmpeg" / "bin",
        root / "runtime" / "bin",
        root / "tools" / "ffmpeg" / "bin",
        root / "tools" / "ffmpeg",
        root / "ffmpeg" / "bin",
    ]


def _from_env(names: tuple[str, ...]) -> list[str]:
    raw = (settings.ffmpeg_path or "").strip()
    if not raw:
        return []
    p = Path(raw)
    out: list[str] = []
    try:
        if p.is_file():
            out.append(str(p))
        elif p.is_dir():
            out.extend(str(p / n) for n in names if (p / n).exists())
    except OSError:
        pass
    return out


def _candidates(names: tuple[str, ...]) -> list[str]:
    out = _from_env(names)
    for d in _search_dirs():
        for n in names:
            p = d / n
            try:
                if p.exists():
                    out.append(str(p))
            except OSError:
                pass
    key = names[0].replace(".exe", "")
    which = shutil.which(key)
    if which:
        out.append(which)
    # 去重但保序
    seen: set[str] = set()
    uniq: list[str] = []
    for c in out:
        k = c.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


def _probe(binary: str, key: str) -> bool:
    """跑一次 -version 确认这个 exe 真的能用（路径存在 ≠ 依赖齐全）。"""
    try:
        r = subprocess.run([binary, "-version"], capture_output=True, timeout=12)
    except Exception:
        return False
    blob = (r.stdout or b"") + (r.stderr or b"")
    return r.returncode == 0 and key.encode() in blob.lower()


def _find(key: str, names: tuple[str, ...]) -> Optional[str]:
    with _PROBE_LOCK:
        if _PROBED[key]:
            return _CACHE[key]
        found: Optional[str] = None
        for cand in _candidates(names):
            if _probe(cand, key):
                found = cand
                break
        _CACHE[key] = found
        _PROBED[key] = True
        return found


def find_ffmpeg() -> Optional[str]:
    return _find("ffmpeg", _NAMES)


def find_ffprobe() -> Optional[str]:
    """优先找同目录的 ffprobe（分发时它们总是一起）；找不到就不勉强。"""
    hit = _find("ffprobe", _PROBE_NAMES)
    if hit:
        return hit
    ff = find_ffmpeg()
    if not ff:
        return None
    sibling = Path(ff).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    if sibling.exists() and _probe(str(sibling), "ffprobe"):
        with _PROBE_LOCK:
            _CACHE["ffprobe"] = str(sibling)
            _PROBED["ffprobe"] = True
        return str(sibling)
    return None


def reset_cache() -> None:
    """改过 FFMPEG_PATH 之后调用，让下次查找重新生效。"""
    with _PROBE_LOCK:
        for k in ("ffmpeg", "ffprobe"):
            _CACHE[k] = None
            _PROBED[k] = False
        _FILTERS.clear()


def status() -> dict:
    """给前端看的可用性。文案直接可展示。"""
    ff = find_ffmpeg()
    if not ff:
        return {
            "available": False,
            "ffmpeg": "",
            "ffprobe": "",
            "version": "",
            "reason": (
                "未找到 ffmpeg。三种解决办法任选其一："
                "① 把 ffmpeg.exe 放进 runtime/ffmpeg/；"
                "② 在 .env 里设 FFMPEG_PATH 指向 ffmpeg.exe 或它所在目录；"
                "③ 把 ffmpeg 加入系统 PATH。"
                "（只影响导出成片，捏人 / 出图 / 做衣服都不受影响）"
            ),
        }
    ver = ""
    try:
        r = subprocess.run([ff, "-version"], capture_output=True, timeout=12)
        lines = (r.stdout or b"").decode("utf-8", "ignore").splitlines()
        ver = lines[0] if lines else ""
    except Exception:
        pass
    return {
        "available": True,
        "ffmpeg": ff,
        "ffprobe": find_ffprobe() or "",
        "version": ver,
        "reason": "",
    }


def has_filter(name: str) -> bool:
    """某个滤镜是否可用（subtitles 依赖 fontconfig，不同构建差别很大）。"""
    if name in _FILTERS:
        return _FILTERS[name]
    ff = find_ffmpeg()
    ok = False
    if ff:
        try:
            r = subprocess.run([ff, "-hide_banner", "-filters"],
                               capture_output=True, timeout=20)
            ok = name.encode() in (r.stdout or b"")
        except Exception:
            ok = False
    _FILTERS[name] = ok
    return ok


class FFmpegError(RuntimeError):
    """ffmpeg 执行失败，消息里带 stderr 尾部（唯一有用的报错）。"""


def run(args: list[str], *, cwd: Optional[Path] = None,
        timeout: float = 600) -> str:
    """执行一条 ffmpeg 命令。失败时抛出带 stderr 的异常。"""
    try:
        proc = subprocess.run(args, cwd=str(cwd) if cwd else None,
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise FFmpegError(f"ffmpeg 超时（{timeout:.0f} 秒）")
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "ignore").strip().splitlines()
        tail = "\n".join(err[-6:]) if err else "（无输出）"
        raise FFmpegError(f"ffmpeg 失败：\n{tail}")
    return (proc.stdout or b"").decode("utf-8", "ignore")


def probe(path: Path | str) -> dict:
    """读媒体信息：时长、有无音轨、采样率。ffprobe 不在时返回空 dict。"""
    fp = find_ffprobe()
    p = Path(path)
    if not fp or not p.exists():
        return {}
    try:
        r = subprocess.run(
            [fp, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(p)],
            capture_output=True, timeout=30,
        )
        data = json.loads((r.stdout or b"{}").decode("utf-8", "ignore") or "{}")
    except Exception:
        return {}
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    try:
        duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {}) or {}
    return {
        "duration": duration,
        "has_audio": has_audio,
        "sample_rate": int(audio.get("sample_rate") or 0) if has_audio else 0,
        "size_mb": round(int(fmt.get("size") or 0) / 1024 / 1024, 2),
    }


def duration_of(path: Path | str, fallback: float = 0.0) -> float:
    """只要时长时的快捷方式。拿不到就用 fallback（不要让渲染因为读不到时长而失败）。"""
    info = probe(path)
    d = float(info.get("duration") or 0.0)
    return d if d > 0.01 else fallback
