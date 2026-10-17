# -*- coding: utf-8 -*-
"""分镜生成 + 首帧出图对接。

这一层的三件事
--------------
1. **问模型要分镜**：把 promptlib 的窄合同发给本地文本模型，拿回 JSON
2. **修**：模型输出不可靠，用 promptlib.normalize 修成合法结构
3. **降级**：模型不可用 / 太慢 / 用户主动关掉时，用规则拼一份能用的

为什么分镜要跑在后台线程
------------------------
本地文本模型在 16G 卡上出 3 格分镜要几分钟。占着一个 HTTP 请求不返回，
前端只能看到转圈，用户会以为卡死。所以这里返回「已开始」，状态写在项目里，
前端轮询同一份数据——不引入第二套任务表，状态只有一个来源。

LLM 一律串行
------------
Qwen3-VL 走 ComfyUI 的 TextGenerate，本身就不支持并发。用一把进程级的锁
串行化，第二个请求排队而不是把显存打爆。

出图走 fusion 而不是重新文生图（关键决策）
------------------------------------------
有定妆照时，每一格都是「以 image_1 的角色为准，把它放进新场景做新动作」，
而不是「再画一个描述里的团子」。这是整部片看起来像同一个角色的唯一低成本
办法，也是团子衣橱已经验证过的同一条通道。
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Callable, Optional

from engine.sdk import engine
from shared.config import settings
from shared.promptlib import shots as shotlib
from shared.promptlib import storyboard as sblib
from shared.promptlib import styles as stylelib

from apps.storyboard.backend import store


class StoryboardError(Exception):
    """业务错误（文案直接给用户看）。"""


# ============================================================
#  画幅
# ============================================================

# src = 出图尺寸（64 对齐，控制显存），out = 成片尺寸（渲染时 cover + 放大）
ASPECTS: dict[str, dict] = {
    "9:16": {"label": "竖屏 9:16", "src": (768, 1344), "out": (1080, 1920)},
    "3:4": {"label": "竖版 3:4", "src": (768, 1024), "out": (1080, 1440)},
    "1:1": {"label": "方形 1:1", "src": (896, 896), "out": (1080, 1080)},
    "16:9": {"label": "横屏 16:9", "src": (1344, 768), "out": (1920, 1080)},
}
DEFAULT_ASPECT = "9:16"


def aspect_of(key: Optional[str]) -> dict:
    return ASPECTS.get(str(key or ""), ASPECTS[DEFAULT_ASPECT])


def aspect_key(key: Optional[str]) -> str:
    k = str(key or "")
    return k if k in ASPECTS else DEFAULT_ASPECT


def output_subdir() -> str:
    return f"storyboard/{datetime.now().strftime('%Y-%m-%d')}"


# ============================================================
#  文本模型
# ============================================================

_LLM_LOCK = threading.Lock()


def llm_available() -> tuple[bool, str]:
    """能不能用本地文本模型。不能用就把原因说清楚，别让用户干等。"""
    try:
        st = engine.text_status()
    except Exception as exc:
        return False, f"文本模型状态读取失败：{exc}"
    if not st.get("available"):
        return False, str(st.get("reason") or "本地文本模型关闭或未就绪")
    return True, ""


def _max_length(count: int) -> int:
    return min(2048, 130 * int(count) + 220)


def generate_beats(project: dict,
                   on_progress: Optional[Callable[[float], None]] = None) -> dict:
    """生成分镜。返回 {beats, title, source, note}，**永远不抛错**。

    抛错意味着用户点了「生成分镜」却什么都没得到；降级至少给他一份能用
    的分镜，他可以逐格改。这是可用性与"正确答案"之间的选择，选可用。
    """
    topic = str(project.get("topic") or "").strip()
    if not topic:
        raise StoryboardError("先写一句选题")

    preset = sblib.preset_key(project.get("preset"))
    # 已有分镜就沿用它的格数（用户可能手改过），否则用预设的默认格数
    count = len(project.get("beats") or []) or len(sblib.preset_of(preset)["slots"])
    total = float(project.get("total_seconds") or 24.0)
    style = stylelib.style_id_of(project.get("style"))
    use_llm = bool(project.get("use_llm", True))

    note = ""
    source = "rule"

    if use_llm:
        ok, why = llm_available()
        if not ok:
            note = f"未使用本地模型：{why}。以下分镜由规则生成，可逐条修改。"
        else:
            system, user = sblib.build_prompt(
                topic=topic, style_id=style, preset=preset, count=count,
                total_seconds=total, tone=str(project.get("tone") or ""),
                extra=str(project.get("extra") or ""),
            )
            try:
                with _LLM_LOCK:
                    text = engine.text_generate(
                        user, system=system, max_length=_max_length(count),
                        temperature=0.7, on_progress=on_progress,
                    )
                raw = sblib.extract_json(text)
                if raw is None:
                    note = "模型输出不是合法 JSON，已用规则补齐。"
                else:
                    result = sblib.normalize(raw, preset=preset, count=count, topic=topic)
                    source = "llm"
                    if result["filled"] < count:
                        note = (f"模型只写了 {result['filled']}/{count} 格，"
                                f"空格已用规则补上。")
                    return {"beats": result["beats"], "title": result["title"],
                            "source": source, "note": note}
            except Exception as exc:
                note = f"本地模型不可用（{type(exc).__name__}: {exc}），已改用规则分镜。"

    fb = sblib.fallback(topic, preset=preset, count=count)
    return {"beats": fb["beats"], "title": fb["title"],
            "source": source, "note": note}


def apply_durations(beats: list[dict], total_seconds: float, xfade: float) -> list[dict]:
    ds = sblib.plan_durations(beats, total_seconds, xfade=xfade)
    for b, d in zip(beats, ds):
        b["duration"] = d
    return beats


# ============================================================
#  出图 payload
# ============================================================

def _base(*, prompt: str, negative: str, width: int, height: int,
          steps: int, seed: int, tag: str) -> dict:
    return {
        "prompt": prompt,
        "negative_prompt": negative or "",
        "width": int(width),
        "height": int(height),
        "steps": int(steps),
        "guidance_scale": float(settings.default_guidance),
        "seed": int(seed),
        "count": 1,
        "strength": 1.0,
        "channel": "auto",
        "output_subdir": output_subdir(),
        "mode": "txt2img",
        "storyboard": {"kind": tag},
    }


def _fusion(payload: dict, ref_data_url: str) -> dict:
    """挂 fusion 编辑通道：CFG 固定 1.0、denoise 1.0、尺寸随 image_1。"""
    payload.update({
        "mode": "img2img",
        "fusion": True,
        "ref_images": [ref_data_url],
        "negative_prompt": "",
        "guidance_scale": 1.0,
        "strength": 1.0,
    })
    return payload


def image_to_data_url(record: dict) -> str:
    """把一张已出好的图读成 data URL，作为编辑通道的 image_1。"""
    import base64
    from pathlib import Path

    # 定妆照记录存的是 image_path，普通出图记录是 path，都接受
    path = str(record.get("path") or record.get("image_path") or "")
    if not path:
        raise StoryboardError("参考图记录里没有文件路径")
    p = Path(path)
    if not p.is_absolute():
        p = Path(".") / p
    if not p.exists():
        raise StoryboardError(f"参考图文件不存在：{p}")
    ext = p.suffix.lower().lstrip(".") or "png"
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"


def payload_cast(project: dict, *, steps: int = 8, seed: int = -1) -> dict:
    """定妆照：整部片的角色身份证。"""
    style = stylelib.style_id_of(project.get("style"))
    built = shotlib.cast_prompt(style, subject=str(project.get("subject") or ""))
    w, h = aspect_of(project.get("aspect"))["src"]
    return _base(prompt=built["prompt"], negative=built["negative"],
                 width=w, height=h, steps=steps, seed=seed, tag="cast")


def payload_shot(project: dict, beat: dict, *, cast_image: Optional[dict] = None,
                 steps: int = 8, seed: int = -1) -> tuple[dict, dict]:
    """单格首帧。返回 (payload, info)。

    有定妆照 → fusion 编辑（稳）；没有 → 文生图（能跑，但角色会飘）。
    """
    style = stylelib.style_id_of(project.get("style"))
    action = str(beat.get("action") or "").strip()
    scene = str(beat.get("scene") or "").strip()
    lock = str(project.get("lock") or "").strip()

    has_cast = bool(cast_image)
    built = shotlib.shot_prompt(
        style_id=style, action=action, shot=str(beat.get("shot") or ""),
        camera=str(beat.get("camera") or ""), scene=scene,
        subject=str(project.get("subject") or ""), lock=lock,
        fusion=has_cast, cast_ref=has_cast,
    )

    w, h = aspect_of(project.get("aspect"))["src"]
    payload = _base(prompt=built["prompt"], negative=built["negative"],
                    width=w, height=h, steps=steps, seed=seed, tag="shot")

    if has_cast:
        try:
            ref = image_to_data_url(cast_image)
        except StoryboardError:
            # 定妆照文件丢了：退回文生图，别让这一格直接失败
            built = shotlib.shot_prompt(
                style_id=style, action=action, shot=str(beat.get("shot") or ""),
                camera=str(beat.get("camera") or ""), scene=scene,
                subject=str(project.get("subject") or ""), lock=lock,
                fusion=False, cast_ref=False)
            payload["prompt"] = built["prompt"]
            payload["negative_prompt"] = built["negative"]
            has_cast = False
        else:
            payload = _fusion(payload, ref)
            try:
                import base64
                from io import BytesIO

                from PIL import Image

                raw = base64.b64decode(ref.split(",", 1)[1])
                with Image.open(BytesIO(raw)) as im:
                    payload["width"], payload["height"] = im.size
            except Exception:
                pass

    info = {"prompt": payload["prompt"], "negative": payload.get("negative_prompt", ""),
            "fusion": has_cast, **built.get("meta", {})}
    return payload, info


# ============================================================
#  选项目录
# ============================================================

def options_payload() -> dict:
    s = shotlib.shot_options()
    ok, why = llm_available()
    return {
        "styles": stylelib.style_options(),
        "presets": [{"id": k, "label": v["label"], "desc": v["desc"],
                     "beats": len(v["slots"])}
                    for k, v in sblib.BEAT_PRESETS.items()],
        "aspects": [{"id": k, "label": v["label"], "out": list(v["out"])}
                    for k, v in ASPECTS.items()],
        "shot_sizes": s["shot_sizes"],
        "cameras": s["cameras"],
        "durations": [12, 18, 24, 30, 40, 60],
        "llm": {"available": ok, "reason": why,
                "model": (engine.text_status() or {}).get("model", "")},
        "defaults": {"style": stylelib.DEFAULT_STYLE, "preset": sblib.DEFAULT_PRESET,
                     "aspect": DEFAULT_ASPECT, "seconds": 24},
    }
