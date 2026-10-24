# -*- coding: utf-8 -*-
"""游戏素材工坊接口 —— 流水线模式。

一次配置（风格 + 时间背景 + 场景/角色/道具清单）→ 一键跑完整条流水线：
    场景基准图 → 其余场景 → 角色 → 道具，逐卡进度可轮询，
    每张素材自动做色调后验校验。单卡可重跑。

一致性三层：风格基因（styles.py）+ 时间背景（presets.py）+ 基准图融合，
流水线里没有任何一步绕开它们，详见 pipeline.py。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import pipeline, store, tonecheck
from .presets import TIME_PRESETS
from .styles import STYLE_PRESETS

router = APIRouter(prefix="/gameassets", tags=["gameassets"])

_MAX_PER_KIND = 6          # 每类素材上限
_MAX_TOTAL = 12            # 单集素材总数上限（一次流水线的合理批量）


class SetIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    style: str = Field(..., description="风格 id，见 /presets")
    time_preset: str = Field(..., description="时间背景 id，见 /presets")
    desc: str = Field("", max_length=300, description="美术风格补充（拼进风格基因）")
    scenes: list[str] = Field(..., min_length=1, description="场景清单，第 1 条作基准图")
    characters: list[str] = Field(default_factory=list, description="角色清单")
    props: list[str] = Field(default_factory=list, description="道具清单")
    threshold: float = Field(tonecheck.DEFAULT_THRESHOLD, ge=10, le=120)

    @staticmethod
    def _clean_lines(lines: list[str], limit: int, label: str) -> list[str]:
        out = []
        for ln in lines[:limit]:
            ln = str(ln).strip()[:200]
            if ln:
                out.append(ln)
        if label == "场景" and not out:
            raise ValueError("场景清单不能为空（第 1 条将作为基准图）")
        return out


@router.get("/presets", summary="风格与时间背景预设")
async def list_presets():
    return {"styles": list(STYLE_PRESETS.values()), "times": list(TIME_PRESETS.values())}


@router.get("/sets", summary="素材集列表")
async def list_sets():
    items = []
    for s in store.list_sets():
        p = s.get("pipeline") or {}
        items.append({
            "id": s["id"], "name": s["name"],
            "style_name": s.get("style_name"), "time_name": s.get("time_name"),
            "status": p.get("status", "idle"),
            "done": p.get("done", 0), "total": p.get("total", 0),
            "asset_count": len(s.get("assets") or []),
            "created_at": s.get("created_at"),
        })
    return {"items": items}


@router.post("/sets", summary="创建素材集并启动流水线（一次配置整包产出）")
async def create_set(req: SetIn):
    try:
        style = STYLE_PRESETS[req.style]
        preset = TIME_PRESETS[req.time_preset]
    except KeyError as exc:
        raise HTTPException(status_code=400, detail={"code": "BAD_PRESET", "message": str(exc)})
    try:
        scenes = SetIn._clean_lines(req.scenes, _MAX_PER_KIND, "场景")
        characters = SetIn._clean_lines(req.characters, _MAX_PER_KIND, "角色")
        props = SetIn._clean_lines(req.props, _MAX_PER_KIND, "道具")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "BAD_BRIEFS", "message": str(exc)})
    total = len(scenes) + len(characters) + len(props)
    if total > _MAX_TOTAL:
        raise HTTPException(status_code=400, detail={
            "code": "TOO_MANY",
            "message": f"单次流水线最多 {_MAX_TOTAL} 张（当前 {total}），请拆成多个素材集"})

    s = store.make_set(
        name=req.name.strip(), style=style["id"], style_name=style["name"],
        time_preset=preset["id"], time_name=preset["name"],
        desc=req.desc.strip(), threshold=req.threshold,
        briefs={"scenes": scenes, "characters": characters, "props": props},
    )
    store.add_set(s)
    pipeline.start(s["id"])     # 后台跑，前端轮询 GET /sets/{sid}
    return s


@router.get("/sets/{sid}", summary="素材集详情（含流水线进度）")
async def get_set(sid: str):
    s = store.get_set(sid)
    if not s:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "素材集不存在"})
    return s


@router.post("/sets/{sid}/pipeline", summary="重跑流水线（覆盖现有素材）")
async def rerun_pipeline(sid: str):
    s = store.get_set(sid)
    if not s:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "素材集不存在"})
    if pipeline.is_running(sid):
        raise HTTPException(status_code=409, detail={"code": "BUSY", "message": "流水线正在运行"})
    try:
        pipeline.start(sid)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail={"code": "BUSY", "message": str(exc)})
    return {"ok": True}


@router.post("/sets/{sid}/assets/{aid}/rerun", summary="重跑单张素材（保持清单与锚点不变）")
async def rerun_asset(sid: str, aid: str):
    from .presets import get_preset
    from .styles import get_style

    s = store.get_set(sid)
    if not s:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "素材集不存在"})
    master = s.get("master")
    if not master:
        raise HTTPException(status_code=409, detail={
            "code": "NEED_MASTER", "message": "基准图缺失，请重跑整条流水线"})
    idx = next((i for i, a in enumerate(s["assets"]) if a["id"] == aid), None)
    if idx is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "素材不存在"})

    old = s["assets"][idx]
    style, preset = get_style(s["style"]), get_preset(s["time_preset"])
    if old["kind"] == "character":
        prompt = pipeline._character_prompt(style, preset, s, old["desc"])
    else:
        prompt = pipeline._asset_prompt(style, preset, s, old["kind_label"],
                                        old["desc"], clean_bg=(old["kind"] == "prop"))
    rec = await pipeline._run_job(prompt=prompt, negative=pipeline._COMMON_NEG,
                                  width=pipeline._ASSET_W, height=pipeline._ASSET_H,
                                  seed=-1, ref_path=master["path"])
    tone = tonecheck.check(rec["path"], master["path"],
                           threshold=float(s.get("threshold") or tonecheck.DEFAULT_THRESHOLD))
    new_asset = {
        "id": old["id"],          # 原位替换，前端不用换 key
        "kind": old["kind"], "kind_label": old["kind_label"], "desc": old["desc"],
        "path": rec["path"], "url": rec["url"], "seed": rec.get("seed"),
        "prompt": prompt, "tone": tone, "created_at": store.now_iso(),
        "rerun_of": aid,
    }

    def _apply(t: dict) -> None:
        t["assets"][idx] = new_asset
    store.mutate(sid, _apply)
    return new_asset


@router.delete("/sets/{sid}", summary="删除素材集")
async def delete_set(sid: str):
    if pipeline.is_running(sid):
        raise HTTPException(status_code=409, detail={"code": "BUSY", "message": "流水线正在运行，不能删除"})
    if not store.delete_set(sid):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "素材集不存在"})
    return {"ok": True}


@router.delete("/sets/{sid}/assets/{aid}", summary="删除素材")
async def delete_asset(sid: str, aid: str):
    removed = {"ok": False}

    def _apply(t: dict) -> None:
        before = len(t["assets"])
        t["assets"] = [a for a in t["assets"] if a["id"] != aid]
        removed["ok"] = len(t["assets"]) < before
    store.mutate(sid, _apply)
    if not removed["ok"]:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "素材不存在"})
    return {"ok": True}


# ============================================================
#  独立运行入口
# ============================================================
def create_app():
    """uvicorn apps.gameassets.backend.routes:create_app --factory --port 8107"""
    from pathlib import Path as _Path

    from fastapi import FastAPI

    from shared.webapp import mount_frontend, mount_media

    application = FastAPI(title="游戏素材工坊", version="2.0.0")
    application.include_router(router, prefix="/api/v1")

    mount_media(application)
    frontend = _Path(__file__).resolve().parents[1] / "frontend"
    mount_frontend(application, frontend, at="/", name="index")
    return application
