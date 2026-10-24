# -*- coding: utf-8 -*-
"""游戏素材工坊接口 3.0 —— 一句话，整包产出。

用户只给一句话需求 + （可选）风格 / 时间背景 / 规模档：
    POST /generate → 后台自动策划（LLM 优先，规则兜底）→ 流水线出图
    （基准图 → 场景×四季 → 角色 → 宠物 → 武器按等级 → 建筑 → 野怪）
前端轮询 GET /sets/{sid} 看进度与结果。

一致性三层：风格基因（styles.py）+ 时间背景（presets.py）+ 基准图融合；
季节变体在同一光照锚下换季，色调阈值放宽 1.6 倍。详见 pipeline.py。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import pipeline, store, tonecheck
from .planner import SCALES
from .presets import TIME_PRESETS, SEASONS, SEASON_ORDER, WEAPON_TIERS, TIER_ORDER
from .styles import STYLE_PRESETS

router = APIRouter(prefix="/gameassets", tags=["gameassets"])


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
    }


@router.post("/generate", summary="一句话 → 自动策划 → 整包生成（后台流水线）")
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
    pipeline.start(s["id"])     # 后台：策划 → 出图，前端轮询
    return s


@router.get("/sets", summary="项目列表")
async def list_sets():
    items = []
    for s in store.list_sets():
        p = s.get("pipeline") or {}
        items.append({
            "id": s["id"], "name": s["name"], "idea": s.get("idea"),
            "style_name": s.get("style_name"), "time_name": s.get("time_name"),
            "status": p.get("status", "idle"),
            "done": p.get("done", 0), "total": p.get("total", 0),
            "asset_count": len(s.get("assets") or []),
            "created_at": s.get("created_at"),
        })
    return {"items": items}


@router.get("/sets/{sid}", summary="项目详情（含方案与流水线进度）")
async def get_set(sid: str):
    s = store.get_set(sid)
    if not s:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "项目不存在"})
    return s


@router.post("/sets/{sid}/pipeline", summary="重跑整条流水线（重新策划 + 覆盖素材）")
async def rerun_pipeline(sid: str):
    s = store.get_set(sid)
    if not s:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "项目不存在"})
    if pipeline.is_running(sid):
        raise HTTPException(status_code=409, detail={"code": "BUSY", "message": "流水线正在运行"})
    try:
        pipeline.start(sid)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail={"code": "BUSY", "message": str(exc)})
    return {"ok": True}


@router.post("/sets/{sid}/assets/{aid}/rerun", summary="重跑单张素材（保持锚点不变）")
async def rerun_asset(sid: str, aid: str):
    s = store.get_set(sid)
    if not s:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "项目不存在"})
    master = s.get("master")
    if not master:
        raise HTTPException(status_code=409, detail={
            "code": "NEED_MASTER", "message": "基准图缺失，请重跑整条流水线"})
    idx = next((i for i, a in enumerate(s["assets"]) if a["id"] == aid), None)
    if idx is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "素材不存在"})

    old = s["assets"][idx]
    item = {"kind": old["kind"], "name": old["name"], "desc": old["desc"],
            "season": old.get("season"), "tier_id": old.get("tier_id")}
    prompt = pipeline.asset_prompt(s, item)
    base = float(s.get("threshold") or tonecheck.DEFAULT_THRESHOLD)
    threshold = base * (pipeline._SEASON_THRESHOLD_FACTOR if old.get("season") else 1.0)
    rec = await pipeline._run_job(prompt=prompt, negative=pipeline._COMMON_NEG,
                                  width=pipeline._ASSET_W, height=pipeline._ASSET_H,
                                  seed=-1, ref_path=master["path"])
    tone = tonecheck.check(rec["path"], master["path"], threshold=threshold)
    new_asset = {**old,
                 "path": rec["path"], "url": rec["url"], "seed": rec.get("seed"),
                 "prompt": prompt, "tone": tone, "created_at": store.now_iso(),
                 "rerun_of": aid}

    def _apply(t: dict) -> None:
        t["assets"][idx] = new_asset
    store.mutate(sid, _apply)
    return new_asset


@router.delete("/sets/{sid}", summary="删除项目")
async def delete_set(sid: str):
    if pipeline.is_running(sid):
        raise HTTPException(status_code=409, detail={"code": "BUSY", "message": "流水线正在运行，不能删除"})
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
def _cleanup_orphan_pipelines() -> None:
    """服务重启后，上一进程没跑完的流水线永远是"生成中"假死——启动时统一标记。"""
    for s in store.list_sets():
        status = (s.get("pipeline") or {}).get("status")
        if status in ("planning", "running") and not pipeline.is_running(s["id"]):
            def _mark(t: dict) -> None:
                t["pipeline"]["status"] = "failed"
                t["pipeline"]["error"] = "服务重启，流水线中断（可点重跑全部续做）"
            store.mutate(s["id"], _mark)
            print(f"[gameassets] 清理中断流水线：{s['id']}")


def create_app():
    """uvicorn apps.gameassets.backend.routes:create_app --factory --port 8107"""
    from pathlib import Path as _Path

    from fastapi import FastAPI

    from shared.webapp import mount_frontend, mount_media

    _cleanup_orphan_pipelines()
    application = FastAPI(title="游戏素材工坊", version="3.0.0")
    application.include_router(router, prefix="/api/v1")

    mount_media(application)
    frontend = _Path(__file__).resolve().parents[1] / "frontend"
    mount_frontend(application, frontend, at="/", name="index")
    return application
