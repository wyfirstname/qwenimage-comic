# -*- coding: utf-8 -*-
"""游戏素材工坊接口 4.0 —— 分类独立生成 + 异常透明。

    POST /generate                      一句话建项目 + 全量编排（plan→master→六类）
    POST /sets/{sid}/stages/{kind}/run  单独生成某类（plan/master/scene/…/monster）
    GET  /sets/{sid}                    详情：stages 各类状态/进度/错误 + 日志 + alive
    POST /sets/{sid}/assets/{aid}/rerun 单卡重跑

异常原则：任何阶段失败都落库并展示（状态 chip + 日志行），前端还有
alive 标志与服务断线检测——绝不出现"页面还在转但其实已经死了"。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import pipeline, store, tonecheck
from .planner import SCALES
from .presets import TIME_PRESETS, SEASONS, SEASON_ORDER, WEAPON_TIERS, TIER_ORDER
from .styles import STYLE_PRESETS

router = APIRouter(prefix="/gameassets", tags=["gameassets"])

_RUNNABLE = set(store.STAGE_IDS) | {"all"}


class GenerateIn(BaseModel):
    idea: str = Field(..., min_length=2, max_length=200,
                      description="一句话描述你想要的游戏世界")
    style: str = Field("ghibli", description="美术风格 id，见 /presets")
    time_preset: str = Field("dusk", description="时间背景 id，见 /presets")
    scale: str = Field("standard", description="规模档：lite | standard | deluxe")
    desc: str = Field("", max_length=300, description="美术风格补充（可选）")
    threshold: float = Field(tonecheck.DEFAULT_THRESHOLD, ge=10, le=120)


@router.get("/presets", summary="风格 / 时间背景 / 规模 / 季节 / 武器等级 预设")
async def list_presets():
    return {
        "styles": list(STYLE_PRESETS.values()),
        "times": list(TIME_PRESETS.values()),
        "scales": [{"id": k, "name": v["name"],
                    "scenes": v["scenes"], "characters": v["characters"],
                    "pets": v["pets"], "weapons": v["weapons"],
                    "buildings": v["buildings"], "monsters": v["monsters"],
                    "images": v["scenes"] * 4 + v["characters"] + v["pets"]
                              + v["weapons"] + v["buildings"] + v["monsters"] + 1}
                   for k, v in SCALES.items()],
        "seasons": [{"id": k, "name": SEASONS[k]["name"]} for k in SEASON_ORDER],
        "tiers": [{"id": k, "name": WEAPON_TIERS[k]["name"]} for k in TIER_ORDER],
        "stages": [{"id": k, "name": v} for k, v in store.STAGES],
    }


@router.post("/generate", summary="一句话 → 建项目 → 全量编排生成（后台队列）")
async def generate(req: GenerateIn):
    if req.style not in STYLE_PRESETS:
        raise HTTPException(status_code=400, detail={
            "code": "BAD_STYLE", "message": f"未知风格：{req.style}"})
    if req.time_preset not in TIME_PRESETS:
        raise HTTPException(status_code=400, detail={
            "code": "BAD_TIME", "message": f"未知时间背景：{req.time_preset}"})
    if req.scale not in SCALES:
        raise HTTPException(status_code=400, detail={
            "code": "BAD_SCALE", "message": f"未知规模：{req.scale}（可选：{', '.join(SCALES)}）"})

    style, preset = STYLE_PRESETS[req.style], TIME_PRESETS[req.time_preset]
    s = store.make_set(
        name=req.idea.strip()[:20], idea=req.idea.strip(),
        style=style["id"], style_name=style["name"],
        time_preset=preset["id"], time_name=preset["name"],
        desc=req.desc.strip(), threshold=req.threshold, scale=req.scale,
    )
    store.add_set(s)
    _enqueue_or_409(s["id"], "all")
    return s


def _enqueue_or_409(sid: str, kind: str) -> None:
    try:
        pipeline.enqueue(sid, kind)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "BUSY" if "已在队列" in str(exc) else "NOT_READY",
            "message": str(exc)})


@router.post("/sets/{sid}/stages/{kind}/run",
             summary="单独生成/重试一个阶段（plan/master/scene/character/pet/weapon/building/monster/all）")
async def run_stage(sid: str, kind: str):
    if not store.get_set(sid):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "项目不存在"})
    if kind not in _RUNNABLE:
        raise HTTPException(status_code=400, detail={
            "code": "BAD_STAGE", "message": f"未知阶段：{kind}（可选：{', '.join(sorted(_RUNNABLE))}）"})
    _enqueue_or_409(sid, kind)
    return {"ok": True}


@router.get("/sets", summary="项目列表")
async def list_sets():
    items = []
    for s in store.list_sets():
        items.append({
            "id": s["id"], "name": s["name"], "idea": s.get("idea"),
            "style_name": s.get("style_name"), "time_name": s.get("time_name"),
            "status": pipeline.overall_status(s),
            "asset_count": len(s.get("assets") or []),
            "created_at": s.get("created_at"),
        })
    return {"items": items}


@router.get("/sets/{sid}", summary="项目详情（阶段状态/进度/错误 + 日志 + alive）")
async def get_set(sid: str):
    s = store.get_set(sid)
    if not s:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "项目不存在"})
    s = dict(s)
    s["alive"] = pipeline.is_busy(sid)      # 页面用它在"队列中/执行中"与真实状态间对账
    s["overall"] = pipeline.overall_status(s)
    return s


@router.post("/sets/{sid}/assets/{aid}/rerun", summary="重跑单张素材（保持锚点不变）")
async def rerun_asset(sid: str, aid: str):
    s = store.get_set(sid)
    if not s:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "项目不存在"})
    if pipeline.is_busy(sid):
        raise HTTPException(status_code=409, detail={"code": "BUSY", "message": "项目有任务在队列中，稍后再试"})
    master = s.get("master")
    if not master:
        raise HTTPException(status_code=409, detail={
            "code": "NEED_MASTER", "message": "基准图缺失，请先生成基准图"})
    idx = next((i for i, a in enumerate(s["assets"]) if a["id"] == aid), None)
    if idx is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "素材不存在"})

    old = s["assets"][idx]
    item = {"kind": old["kind"], "name": old["name"], "desc": old["desc"],
            "season": old.get("season"), "tier_id": old.get("tier_id")}
    prompt = pipeline.asset_prompt(s, item)
    base = float(s.get("threshold") or tonecheck.DEFAULT_THRESHOLD)
    threshold = base * (pipeline._SEASON_THRESHOLD_FACTOR if old.get("season") else 1.0)
    try:
        rec = await pipeline._run_job(prompt=prompt, negative=pipeline._COMMON_NEG,
                                      width=pipeline._ASSET_W, height=pipeline._ASSET_H,
                                      seed=-1, ref_path=master["path"])
    except Exception as exc:
        store.append_log(sid, "error", f"重跑「{old['name']}」失败：{exc}")
        raise HTTPException(status_code=500, detail={"code": "GEN_FAILED", "message": str(exc)[:200]})
    tone = tonecheck.check(rec["path"], master["path"], threshold=threshold)
    new_asset = {**old,
                 "path": rec["path"], "url": rec["url"], "seed": rec.get("seed"),
                 "prompt": prompt, "tone": tone, "created_at": store.now_iso(),
                 "rerun_of": aid}

    def _apply(t: dict) -> None:
        t["assets"][idx] = new_asset
    store.mutate(sid, _apply)
    store.append_log(sid, "info", f"重跑「{old['name']}」完成 色调{tone['score']:.0f}")
    return new_asset


@router.delete("/sets/{sid}", summary="删除项目")
async def delete_set(sid: str):
    if pipeline.is_busy(sid):
        raise HTTPException(status_code=409, detail={"code": "BUSY", "message": "项目有任务在队列中，不能删除"})
    if not store.delete_set(sid):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "项目不存在"})
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

    application = FastAPI(title="游戏素材工坊", version="4.0.0")
    application.include_router(router, prefix="/api/v1")

    store.recover_interrupted()     # 启动时一次性清理上次进程留下的假 running
    mount_media(application)
    frontend = _Path(__file__).resolve().parents[1] / "frontend"
    mount_frontend(application, frontend, at="/", name="index")
    return application
