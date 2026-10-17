# -*- coding: utf-8 -*-
"""成片渲染：静帧 → 动效片段 → 拼成一条竖屏 mp4。

这一层只做「运动」和「拼接」，不碰模型。所以它可以在任何机器上跑，
包括那台 6G 显存的机器——出图慢是出图的事，渲染是 CPU 的事。

三个阶段
--------
1. 每格：一张静帧 + 一条镜头运动 → 一段 h264
2. 拼接：xfade 交叉淡入，一段接一段
3. 收尾：音轨（有旁白配音就叠，没有就铺静音）+ 可选烧字幕

三个踩过的坑，这里都绕开了
--------------------------
* **zoompan 不能用 `-loop 1` 输入**。zoompan 的 `on`（输出帧序号）在多个输入帧
  之间是累计的，`-loop 1` 会让第二帧输入时 `on` 从 125 接着涨，推拉直接失控。
  正确做法是单帧输入 + `d=总帧数` + `-frames:v` 卡死长度。
* **抖动**。zoompan 在低分辨率输入上做亚像素移动会顿。所以先 scale 到 2 倍
  画幅再 zoompan，位移步长变成半像素级，缩放回输出尺寸后就看不出来了。
* **Windows 上 subtitles 滤镜的路径转义**。`C:\\a\\b.srt` 里的冒号和反斜杠会
  把 filtergraph 拆坏。做法是把进程 cwd 设成临时目录，filter 里只写文件名。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from shared.config import settings
from shared.promptlib import shots as shotlib

from apps.storyboard.backend import store
from apps.storyboard.backend.service import aspect_of


class RenderError(Exception):
    """渲染失败，消息直接给用户看。"""


FPS = 30
XFADE = 0.5
MAX_SEGMENTS = 16

_ROOT = Path(__file__).resolve().parents[3]

_FFMPEG_LOCK = threading.Lock()
_FFMPEG_CACHE: Optional[str] = None
_FFMPEG_PROBED = False


# ============================================================
#  找 ffmpeg
# ============================================================

def _candidates() -> list[str]:
    names = ["ffmpeg.exe", "ffmpeg"] if os.name == "nt" else ["ffmpeg"]
    dirs = [
        _ROOT / "runtime" / "ffmpeg" / "bin",   # 便携包
        _ROOT / "runtime" / "bin",
        _ROOT / "tools" / "ffmpeg" / "bin",
        _ROOT / "ffmpeg" / "bin",
        _ROOT,
    ]
    out: list[str] = []
    for d in dirs:
        for n in names:
            p = d / n
            if p.exists():
                out.append(str(p))
    which = shutil.which("ffmpeg")
    if which:
        out.append(which)
    return out


def find_ffmpeg() -> Optional[str]:
    """找可用的 ffmpeg。结果缓存——每次渲染都扫盘没必要。"""
    global _FFMPEG_CACHE, _FFMPEG_PROBED
    with _FFMPEG_LOCK:
        if _FFMPEG_PROBED:
            return _FFMPEG_CACHE
        for cand in _candidates():
            try:
                r = subprocess.run([cand, "-version"], capture_output=True, timeout=12)
            except Exception:
                continue
            if r.returncode == 0 and b"ffmpeg" in (r.stdout or b"").lower():
                _FFMPEG_CACHE = cand
                break
        _FFMPEG_PROBED = True
        return _FFMPEG_CACHE


def ffmpeg_status() -> dict:
    ff = find_ffmpeg()
    if not ff:
        return {"available": False,
                "reason": "未找到 ffmpeg。把它放进 runtime/ffmpeg/bin/ 或加入 PATH 即可导出成片（其余功能不受影响）。"}
    ver = ""
    try:
        r = subprocess.run([ff, "-version"], capture_output=True, timeout=12)
        first = (r.stdout or b"").decode("utf-8", "ignore").splitlines()[:1]
        ver = first[0] if first else ""
    except Exception:
        pass
    return {"available": True, "path": ff, "version": ver}


_FILTER_CACHE: dict[str, bool] = {}


def has_filter(name: str) -> bool:
    """某个 ffmpeg 滤镜在不在。缓存结果——每次渲染都扫一遍 -filters 太浪费。"""
    if name in _FILTER_CACHE:
        return _FILTER_CACHE[name]
    ff = find_ffmpeg()
    if not ff:
        _FILTER_CACHE[name] = False
        return False
    try:
        r = subprocess.run([ff, "-hide_banner", "-filters"], capture_output=True,
                           timeout=20)
        ok = name.encode() in (r.stdout or b"")
    except Exception:
        ok = False
    _FILTER_CACHE[name] = ok
    return ok


# ============================================================
#  工具
# ============================================================

def _run(args: list[str], *, cwd: Path, timeout: float = 600) -> str:
    """跑一条 ffmpeg，失败时把 stderr 尾部抛出来（这是唯一有用的报错）。"""
    try:
        proc = subprocess.run(args, cwd=str(cwd), capture_output=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RenderError(f"ffmpeg 超时（{timeout:.0f} 秒），可能是图片损坏或分辨率过大")
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "ignore").strip().splitlines()
        tail = "\n".join(err[-6:]) if err else "（无输出）"
        raise RenderError(f"ffmpeg 失败：\n{tail}")
    return (proc.stdout or b"").decode("utf-8", "ignore")


def _even(v: int) -> int:
    return int(v) // 2 * 2


def _srt_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _clean_text(s: str) -> str:
    """字幕文本里不能有换行和 & < >，ffmpeg 会当成标记。"""
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _resolve(path_str: str) -> Path:
    p = Path(str(path_str))
    if not p.is_absolute():
        p = Path(".") / p
    return p


# ============================================================
#  单段
# ============================================================

def build_segment(*, image: Path, out: Path, duration: float, camera: str,
                  out_w: int, out_h: int, cwd: Path) -> None:
    ff = find_ffmpeg()
    if not ff:
        raise RenderError("未找到 ffmpeg，无法渲染")

    frames = max(2, int(round(duration * FPS)))
    # 2 倍超采样：位移步长落到半像素级，缩放回输出尺寸后抖动不可见
    sw, sh = _even(out_w * 2), _even(out_h * 2)

    move = shotlib.move_of(camera)
    z_tpl = move["z"]
    if z_tpl == "1.12":
        z = "1.12"          # 平移类：固定放大一点，留出可移动的余量
    else:
        z = z_tpl.replace("{delta}", "0.14").replace("{d}", str(frames))
    x = move["x"].replace("{d}", str(frames))
    y = move["y"].replace("{d}", str(frames))

    vf = (
        f"scale={sw}:{sh}:force_original_aspect_ratio=increase,"
        f"crop={sw}:{sh},"
        # 单帧输入 + d=帧数：避免 -loop 1 让 on 跨帧累计（见模块注释）
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:"
        f"s={out_w}x{out_h}:fps={FPS},"
        f"format=yuv420p"
    )
    _run([ff, "-y", "-hide_banner", "-loglevel", "error",
          "-i", image.name,
          "-vf", vf,
          "-frames:v", str(frames),
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
          "-pix_fmt", "yuv420p",
          out.name], cwd=cwd)


def concat_segments(*, segs: list[dict], out: Path, xfade: float,
                    audio: Optional[Path], srt: Optional[Path],
                    out_w: int, out_h: int, cwd: Path,
                    burn_subtitles: bool) -> str:
    """xfade 串起所有片段，叠音轨，可选烧字幕。返回实际用到的策略说明。"""
    ff = find_ffmpeg()
    n = len(segs)
    total = sum(s["duration"] for s in segs) - (n - 1) * xfade

    # 参数组装必须严格分三段：先所有输入，再 filter_complex，最后所有输出选项。
    # ffmpeg 按位置判断某个选项属于输入还是输出，-map 一旦出现在 -i 之前就会被
    # 当成给下一个输入用的，直接报 "cannot be applied to input url"。
    args = [ff, "-y", "-hide_banner", "-loglevel", "error"]
    for s in segs:
        args += ["-i", s["file"]]

    use_voice = bool(audio and audio.exists())
    if use_voice:
        args += ["-i", audio.name]
    else:
        # 没有配音也要有一条音轨：不少播放器和平台对「无音轨」的处理不一致
        args += ["-f", "lavfi", "-t", f"{total:.2f}",
                 "-i", "anullsrc=r=44100:cl=stereo"]
    audio_idx = n

    filters: list[str] = []
    note = ""

    if n == 1:
        filters.append("[0:v]setpts=PTS-STARTPTS[vout]")
        vlabel = "[vout]"
        note = "单镜头"
    else:
        # offset 累计：第 k 次 xfade 的起点 = 上一段结束 - 转场时长
        offset = 0.0
        prev = "[0:v]"
        for i in range(1, n):
            offset += segs[i - 1]["duration"] - xfade
            label = f"[v{i}]"
            filters.append(
                f"{prev}[{i}:v]xfade=transition=fade:duration={xfade:.2f}:"
                f"offset={offset:.3f}{label}"
            )
            prev = label
        vlabel = prev
        note = f"{n} 段交叉淡入"

    if burn_subtitles and srt and has_filter("subtitles"):
        style = (
            "FontName=Microsoft YaHei,FontSize=16,PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H66000000,BorderStyle=1,Outline=2,Shadow=0,"
            "Alignment=2,MarginV=72"
        )
        # cwd 已设为临时目录 → 这里只写文件名，避开 Windows 路径转义
        filters.append(
            f"{vlabel}subtitles={srt.name}:force_style='{style}'[vsub]"
        )
        vlabel = "[vsub]"

    filters.append(f"{vlabel}format=yuv420p[vfin]")

    args += ["-filter_complex", ";".join(filters)]
    args += ["-map", "[vfin]", "-map", f"{audio_idx}:a"]
    args += ["-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k" if use_voice else "96k",
             "-movflags", "+faststart", "-shortest",
             out.name]

    try:
        _run(args, cwd=cwd, timeout=900)
    except RenderError:
        if burn_subtitles and srt:
            # 烧字幕是最容易因字体缺失而失败的一步，单独降级重试一次
            return concat_segments(segs=segs, out=out, xfade=xfade, audio=audio,
                                   srt=srt, out_w=out_w, out_h=out_h, cwd=cwd,
                                   burn_subtitles=False) + "（字幕烧制失败，已跳过）"
        raise
    return note


def write_srt(*, segs: list[dict], path: Path, xfade: float) -> None:
    """按 xfade 压缩后的真实时间轴写字幕。

    段 i 的字幕从它的 xfade 起点开始，到下一段起点为止；最后一段留到片尾，
    因为转场吃掉的那半秒里，画面上仍然是它在淡出。
    """
    lines: list[str] = []
    t = 0.0
    total = sum(s["duration"] for s in segs) - (len(segs) - 1) * xfade
    for i, s in enumerate(segs):
        start = t
        end = total if i == len(segs) - 1 else t + s["duration"] - xfade
        text = _clean_text(s.get("narration") or "")
        if text and end > start:
            lines.append(f"{i + 1}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n")
        t = start + s["duration"] - xfade
    path.write_text("\n".join(lines), encoding="utf-8")


# ============================================================
#  主流程
# ============================================================

def render_project(project: dict, *,
                   on_progress: Optional[Callable[[float, str], None]] = None,
                   burn_subtitles: bool = True) -> dict:
    """把项目渲染成 mp4。同步阻塞，调用方负责放到后台线程。

    只渲染「已经出好图」的格子；没图的跳过并在结果里说明，而不是整体失败。
    用户常常是边出图边想先看一眼效果。
    """
    def step(p: float, msg: str) -> None:
        if on_progress:
            try:
                on_progress(max(0.0, min(1.0, p)), msg)
            except Exception:
                pass

    status = ffmpeg_status()
    if not status["available"]:
        raise RenderError(status["reason"])

    beats = [b for b in (project.get("beats") or []) if b.get("image_url")]
    if not beats:
        raise RenderError("还没有任何一格的画面，先把分镜出成图再渲染")
    beats = sorted(beats, key=lambda b: int(b.get("index") or 0))[:MAX_SEGMENTS]

    skipped = len([b for b in (project.get("beats") or [])]) - len(beats)
    aspect = aspect_of(project.get("aspect"))
    out_w, out_h = aspect["out"]
    xfade = min(float(project.get("xfade") or XFADE), 1.0)

    pid = str(project.get("id") or "sb")
    tmp = store.data_dir() / "_tmp" / pid
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    out_dir = store.outputs_dir() / datetime.now().strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"{pid}.mp4"

    try:
        segs: list[dict] = []
        total_n = len(beats)
        for i, b in enumerate(beats):
            step(0.02 + 0.78 * (i / max(1, total_n)),
                 f"渲染第 {i + 1}/{total_n} 格…")
            src = _resolve(_image_path(b))
            if not src.exists():
                raise RenderError(f"第 {b.get('index')} 格的图片文件不存在：{src}")
            # 图片名统一成 imgN.png，避免中文名/空格进 filtergraph
            local = tmp / f"img{i}.png"
            shutil.copyfile(src, local)
            seg_name = f"seg{i}.mp4"
            build_segment(image=local, out=tmp / seg_name,
                          duration=float(b.get("duration") or 3.0),
                          camera=str(b.get("camera") or ""),
                          out_w=out_w, out_h=out_h, cwd=tmp)
            segs.append({"file": seg_name,
                         "duration": float(b.get("duration") or 3.0),
                         "narration": b.get("narration") or ""})

        step(0.82, "拼接片段…")
        srt = tmp / "sub.srt"
        write_srt(segs=segs, path=srt, xfade=xfade)

        audio = None
        voice = project.get("voice") or {}
        if voice.get("path"):
            ap = _resolve(voice["path"])
            if ap.exists():
                shutil.copyfile(ap, tmp / "voice.m4a")
                audio = tmp / "voice.m4a"

        note = concat_segments(segs=segs, out=tmp / "final.mp4", xfade=xfade,
                               audio=audio, srt=srt, out_w=out_w, out_h=out_h,
                               cwd=tmp, burn_subtitles=burn_subtitles)

        step(0.95, "写出文件…")
        shutil.move(str(tmp / "final.mp4"), str(final))
        # 字幕始终另存一份，用户拿去别处用
        try:
            shutil.copyfile(srt, out_dir / f"{pid}.srt")
        except Exception:
            pass

        size_mb = round(final.stat().st_size / 1024 / 1024, 2)
        duration = round(sum(s["duration"] for s in segs) - (len(segs) - 1) * xfade, 2)
        step(1.0, "完成")

        rel = final.relative_to(settings.data_dir_path).as_posix()
        msg = note
        if skipped > 0:
            msg = (msg + "；" if msg else "") + f"跳过 {skipped} 格未出图的镜头"
        return {"path": str(final), "url": f"/data/{rel}", "duration": duration,
                "size_mb": size_mb, "segments": len(segs), "note": msg,
                "skipped": skipped, "subtitles": True, "has_voice": bool(audio)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _image_path(beat: dict) -> str:
    """格子记录里优先用本地路径，其次由 /data 的 url 反推。"""
    p = str(beat.get("image_path") or "")
    if p:
        return p
    url = str(beat.get("image_url") or "")
    if url.startswith("/data/"):
        return str(settings.data_dir_path / url[len("/data/"):])
    return url
