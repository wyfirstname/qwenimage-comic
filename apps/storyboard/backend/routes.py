# -*- coding: utf-8 -*-
"""团子小剧场接口。

流程是一条线：
    写选题 → 生成分镜（规则秒出，可选 AI 重写）→ 出定妆照
           → 逐格出首帧 → 渲染成片

两个慢活（AI 重写、渲染）都跑在后台线程，状态写在项目里，前端轮询
GET /projects/{id} 就够了——不引入第二套任务体系。
出图走引擎串行队列 + job hook 自动回填，见 hooks.py。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator

from engine.sdk import engine, GenerationError
from shared import repo
from shared.models_util import new_id

from apps.storyboard.backend import render, store
from apps.storyboard.backend import service as svc
from apps.storyboard.backend.service import StoryboardError
from shared.promptlib import shots as shotlib

router = APIRouter(tags=["storyboard"], prefix="/storyboard")


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400,
                         detail={"code": "BAD_REQUEST", "message": str(exc)})


def _not_found(what: str = "项目") -> HTTPException:
    return HTTPException(status_code=404,
                         detail={"code": "NOT_FOUND", "message": f"{what}不存在"})


def _load(pid: str) -> dict:
    p = store.get_project(pid)
    if not p:
        raise _not_found()
    return p


# ============================================================
#  请求模型
# ============================================================

class ProjectIn(BaseModel):
    topic: str = Field(..., min_length=1, max_length=500)
    title: str = Field("", max_length=24)
    style: str = "dango_soft"
    preset: str = "3act"
    tone: str = Field("", max_length=80)
    extra: str = Field("", max_length=300)
    subject: str = Field("", max_length=200)
    aspect: str = "9:16"
    total_seconds: float = Field(24.0, ge=6.0, le=180.0)
    use_llm: bool = True


class ProjectPatch(BaseModel):
    title: Optional[str] = Field(None, max_length=24)
    topic: Optional[str] = Field(None, min_length=1, max_length=500)
    style: Optional[str] = None
    preset: Optional[str] = None
    tone: Optional[str] = Field(None, max_length=80)
    extra: Optional[str] = Field(None, max_length=300)
    subject: Optional[str] = Field(None, max_length=200)
    lock: Optional[str] = Field(None, max_length=300)
    aspect: Optional[str] = None
    total_seconds: Optional[float] = Field(None, ge=6.0, le=180.0)
    use_llm: Optional[bool] = None


class BeatIn(BaseModel):
    narration: str = Field("", max_length=60)
    action: str = Field("", max_length=60)
    scene: str = Field("", max_length=120)
    shot: str = "中景"
    camera: str = "push_in"
    duration: Optional[float] = Field(None, ge=1.0, le=20.0)


class BeatsIn(BaseModel):
    beats: List[BeatIn] = Field(..., min_length=1, max_length=render.MAX_SEGMENTS)


class RenderIn(BaseModel):
    burn_subtitles: bool = True


class GenIn(BaseModel):
    steps: int = Field(8, ge=1, le=60)
    seed: int = -1


# ============================================================
#  元信息
# ============================================================

@router.get("/options", summary="画风 / 叙事模板 / 画幅 / 镜头选项")
def options():
    out = svc.options_payload()
    out["ffmpeg"] = render.ffmpeg_status()
    return out


@router.get("/status", summary="ffmpeg 与文本模型状态")
def status():
    return {"ffmpeg": render.ffmpeg_status(), "llm": svc.llm_available()[0]}


# ============================================================
#  项目
# ============================================================

def _summarize(p: dict) -> dict:
    beats = p.get("beats") or []
    return {
        "id": p["id"], "title": p.get("title") or "", "topic": p.get("topic") or "",
        "style": p.get("style"), "preset": p.get("preset"), "aspect": p.get("aspect"),
        "total_seconds": p.get("total_seconds"),
        "beats_count": len(beats),
        "beats_ready": sum(1 for b in beats if b.get("status") == "ready"),
        "cast_status": (p.get("cast") or {}).get("status"),
        "render_status": (p.get("render") or {}).get("status"),
        "render_url": (p.get("render") or {}).get("url") or "",
        "updated_at": p.get("updated_at"),
    }


@router.get("/projects", summary="项目列表")
def list_projects():
    return {"projects": [_summarize(p) for p in store.list_projects()],
            "stats": store.stats()}


@router.post("/projects", summary="新建项目（一句话，立刻得到一版规则分镜）")
async def create_project(req: ProjectIn):
    aspect_key = svc.aspect_key(req.aspect)
    aw, ah = svc.aspect_of(aspect_key)["out"]
    project = store.make_project(
        topic=req.topic.strip(), style=req.style, preset=req.preset,
        tone=req.tone, extra=req.extra, subject=req.subject,
        total_seconds=req.total_seconds, aspect=aspect_key,
        width=aw, height=ah, use_llm=req.use_llm,
    )
    # 创建即给一版规则分镜：用户马上有东西可看可改，AI 重写是可选的下一步
    try:
        result = svc.generate_beats(project)
    except StoryboardError as exc:
        raise _bad(exc)
    _apply_beats(project, result)
    store.add_project(project)
    return project


@router.get("/projects/{pid}", summary="项目详情（轮询这个）")
def get_project(pid: str):
    return _load(pid)


@router.patch("/projects/{pid}", summary="修改项目设定")
async def patch_project(pid: str, req: ProjectPatch):
    p = _load(pid)
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if "aspect" in fields:
        key = svc.aspect_key(fields.pop("aspect"))
        fields["aspect"] = key
        aw, ah = svc.aspect_of(key)["out"]
        fields["width"], fields["height"] = aw, ah
    # 时长变了 → 重新分配每格时长
    if "total_seconds" in fields:
        beats = p.get("beats") or []
        if beats:
            svc.apply_durations(beats, float(fields["total_seconds"]),
                                float(p.get("xfade") or render.XFADE))
    updated = store.update_project(pid, **fields)
    return updated


@router.delete("/projects/{pid}", summary="删除项目")
async def delete_project(pid: str):
    p = store.delete_project(pid)
    if not p:
        raise _not_found()
    return {"deleted": pid}


# ============================================================
#  分镜
# ============================================================

def _apply_beats(project: dict, result: dict) -> None:
    """把 generate_beats 的结果写进项目结构（含时长分配）。"""
    xfade = float(project.get("xfade") or render.XFADE)
    beats = []
    for i, b in enumerate(result["beats"], 1):
        beat = store.new_beat(i)
        beat.update({
            "beat": b.get("beat") or "", "narration": b.get("narration") or "",
            "action": b.get("action") or "", "shot": b.get("shot") or "中景",
            "camera": b.get("camera") or "push_in",
        })
        beats.append(beat)
    svc.apply_durations(beats, float(project.get("total_seconds") or 24.0), xfade)
    project["beats"] = beats
    if result.get("title") and not project.get("title"):
        project["title"] = result["title"]
    project["beats_source"] = result.get("source") or "rule"


def _gen_worker(pid: str) -> None:
    project = store.get_project(pid)
    if not project:
        return
    last = [0.0]

    def on_progress(v: float) -> None:
        if v - last[0] >= 0.05 or v >= 1.0:
            last[0] = v
            store.update_project(pid, llm_progress=round(float(v), 2))

    try:
        result = svc.generate_beats(project, on_progress=on_progress)
    except StoryboardError as exc:
        store.update_project(pid, beats_state="failed", beats_note=str(exc))
        return
    except Exception as exc:  # 模型层意外崩溃也要让界面恢复可操作
        store.update_project(pid, beats_state="failed",
                             beats_note=f"生成分镜出错：{exc}")
        return

    def _fn(p: dict) -> None:
        # 保留已经出好图的格子：重写文案不应该把画也擦了
        old = {int(b.get("index") or 0): b for b in (p.get("beats") or [])}
        fresh = []
        for i, b in enumerate(result["beats"], 1):
            beat = store.new_beat(i)
            beat.update({k: b.get(k) or "" for k in
                         ("beat", "narration", "action", "shot", "camera")})
            prev = old.get(i)
            if prev and prev.get("image_id"):
                for k in ("image_id", "image_url", "image_path", "status", "error"):
                    beat[k] = prev.get(k) or ""
            fresh.append(beat)
        result["beats"] = fresh
        svc.apply_durations(fresh, float(p.get("total_seconds") or 24.0),
                            float(p.get("xfade") or render.XFADE))
        p["beats"] = fresh
        if result.get("title"):
            p["title"] = result["title"]
        p["beats_source"] = result.get("source") or "rule"

    store.mutate(pid, _fn)
    store.update_project(pid, beats_state="done", llm_progress=1.0,
                         beats_note=result.get("note") or "")


@router.post("/projects/{pid}/beats/regenerate",
             summary="让本地模型重写分镜（后台，轮询项目看 beats_state）")
async def regenerate_beats(pid: str):
    p = _load(pid)
    if p.get("beats_state") == "generating":
        return {"status": "generating", "progress": p.get("llm_progress") or 0}
    if not (p.get("topic") or "").strip():
        raise _bad(StoryboardError("先写一句选题"))
    store.update_project(pid, beats_state="generating", llm_progress=0.0,
                         beats_note="")
    threading.Thread(target=_gen_worker, args=(pid,), daemon=True).start()
    return {"status": "generating"}


@router.put("/projects/{pid}/beats", summary="手动保存分镜（可增删格、改文案）")
async def save_beats(pid: str, req: BeatsIn):
    p = _load(pid)
    old = {int(b.get("index") or 0): b for b in (p.get("beats") or [])}

    def _fn(pr: dict) -> None:
        fresh = []
        for i, b in enumerate(req.beats, 1):
            beat = store.new_beat(i)
            beat.update({
                "narration": b.narration.strip(), "action": b.action.strip(),
                "scene": b.scene.strip(),
                "shot": shotlib.shot_size_of(b.shot),
                "camera": shotlib.move_key(b.camera),
                "duration": float(b.duration) if b.duration else beat["duration"],
            })
            prev = old.get(i)
            if prev and prev.get("image_id"):
                for k in ("image_id", "image_url", "image_path", "status", "error"):
                    beat[k] = prev.get(k) or ""
            fresh.append(beat)
        svc.apply_durations(fresh, float(pr.get("total_seconds") or 24.0),
                            float(pr.get("xfade") or render.XFADE))
        pr["beats"] = fresh

    updated = store.mutate(pid, _fn)
    store.update_project(pid, beats_state="done")
    return updated


# ============================================================
#  出图：定妆照 / 首帧
# ============================================================

async def _submit(payload: dict) -> dict:
    payload["job_id"] = new_id("job")
    try:
        created = await engine.submit_batch(payload)
    except GenerationError as exc:
        raise HTTPException(status_code=409,
                            detail={"code": "BUSY", "message": str(exc)})
    est = {}
    try:
        est = engine.estimate(payload)
    except Exception:
        est = {}
    return {**created, "job_id": payload["job_id"], "prompt": payload["prompt"],
            "estimate": est}


@router.post("/projects/{pid}/cast", summary="出定妆照（角色身份证）")
async def make_cast(pid: str, req: GenIn):
    p = _load(pid)
    payload = svc.payload_cast(p, steps=req.steps, seed=req.seed)
    payload["storyboard"] = {"pid": pid, "kind": "cast"}
    store.mutate(pid, store.set_cast(status="imaging", error="",
                                     prompt=payload["prompt"]))
    out = await _submit(payload)
    store.mutate(pid, store.set_cast(job_id=payload["job_id"]))
    return out


@router.post("/projects/{pid}/shots/{index}", summary="出某一格的首帧")
async def make_shot(pid: str, index: int, req: GenIn):
    p = _load(pid)
    beat = next((b for b in (p.get("beats") or [])
                 if int(b.get("index") or 0) == index), None)
    if not beat:
        raise _not_found("分镜格")
    if not (beat.get("action") or "").strip() and not (beat.get("narration") or "").strip():
        raise _bad(StoryboardError("这一格还没有画面描述，先写一句动作"))

    cast_image = None
    if (p.get("cast") or {}).get("status") == "ready":
        cast_image = p["cast"]

    payload, info = svc.payload_shot(p, beat, cast_image=cast_image,
                                     steps=req.steps, seed=req.seed)
    payload["storyboard"] = {"pid": pid, "kind": "shot", "index": index}
    store.set_beat_image(pid, index, status="imaging", error="", job_id="",
                         prompt=info["prompt"], fusion=info.get("fusion", False))
    out = await _submit(payload)
    return {**out, "fusion": info.get("fusion", False)}


@router.get("/jobs/{job_id}", summary="任务详情（含出图）")
def job_detail(job_id: str):
    job = repo.get_job(job_id)
    if not job:
        raise _not_found("任务")
    job = dict(job)
    job["images"] = repo.images_by_job(job_id)
    return job


# ============================================================
#  渲染
# ============================================================

def _render_worker(pid: str, burn: bool) -> None:
    def _on_progress(p: float, msg: str) -> None:
        store.mutate(pid, lambda pr: pr["render"].update(
            progress=round(p, 3), message=msg))

    try:
        result = render.render_project(store.get_project(pid) or {},
                                       on_progress=_on_progress,
                                       burn_subtitles=burn)
        store.mutate(pid, lambda pr: pr["render"].update(
            status=store.RENDER_DONE, progress=1.0, message=result.get("note") or "",
            path=result["path"], url=result["url"], duration=result["duration"],
            size_mb=result["size_mb"], error="", finished_at=store.now_iso()))
    except Exception as exc:
        store.mutate(pid, lambda pr: pr["render"].update(
            status=store.RENDER_FAILED, error=str(exc), finished_at=store.now_iso()))


@router.post("/projects/{pid}/render", summary="渲染成片（后台）")
async def render_project(pid: str, req: RenderIn):
    p = _load(pid)
    if (p.get("render") or {}).get("status") == store.RENDER_RUNNING:
        return {"status": store.RENDER_RUNNING}
    if not render.find_ffmpeg():
        st = render.ffmpeg_status()
        raise _bad(Exception(st.get("reason") or "未找到 ffmpeg"))
    store.mutate(pid, lambda pr: pr.update(render=store.new_render()))
    store.mutate(pid, lambda pr: pr["render"].update(
        status=store.RENDER_RUNNING, started_at=store._now()))
    threading.Thread(target=_render_worker, args=(pid, req.burn_subtitles),
                     daemon=True).start()
    return {"status": store.RENDER_RUNNING}
