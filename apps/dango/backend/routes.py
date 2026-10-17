# -*- coding: utf-8 -*-
"""团子衣橱接口。

所有出图都走 `engine.submit_batch`（平台的串行队列），不在请求里阻塞等待——
一台机器同一时刻只该跑一个推理。前端拿到 job_id 后轮询 `/dango/jobs/{id}`。

接口分四组：
    捏团子   POST /dango/characters          文生图出素体
    做衣服   POST /dango/outfits/design      文生图出单品图
    换装     POST /dango/wear                多参考图编辑（fusion 通道）
    微调     POST /dango/tweak               单图局部编辑
另外一组是「资料库」的增删改（commit / adopt / restore / delete）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from engine.sdk import engine, GenerationError
from shared import repo
from shared.models_util import new_id
from apps.dango.backend import service as svc
from apps.dango.backend import store
from apps.dango.backend.service import DangoError

router = APIRouter(tags=["dango"], prefix="/dango")


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400,
                         detail={"code": "BAD_REQUEST", "message": str(exc)})


def _not_found(what: str) -> HTTPException:
    return HTTPException(status_code=404,
                         detail={"code": "NOT_FOUND", "message": f"{what}不存在"})


# ============================================================
#  请求模型
# ============================================================

class GenBase(BaseModel):
    steps: int = Field(8, ge=1, le=60)
    seed: int = -1
    count: int = Field(1, ge=1, le=4)


class CharacterIn(GenBase):
    name: str = ""
    params: Dict[str, Any] = Field(default_factory=dict)
    width: int = Field(768, ge=256, le=2048)
    height: int = Field(1152, ge=256, le=2048)

    @field_validator("width", "height")
    @classmethod
    def _mul64(cls, v: int) -> int:
        if v % 64:
            raise ValueError("尺寸必须是 64 的整数倍")
        return v


class OutfitDesignIn(GenBase):
    description: str = ""
    slot: str = "full"
    style: str = "mochi"
    name: str = ""
    width: int = Field(768, ge=256, le=2048)
    height: int = Field(768, ge=256, le=2048)

    @field_validator("width", "height")
    @classmethod
    def _mul64(cls, v: int) -> int:
        if v % 64:
            raise ValueError("尺寸必须是 64 的整数倍")
        return v


class WearIn(GenBase):
    character_id: str
    outfit_ids: List[str] = Field(default_factory=list)
    slot: str = "full"
    extra: str = ""
    max_mp: float = Field(0.9, ge=0.2, le=2.0)


class TweakIn(GenBase):
    character_id: str
    tweak: str
    value: str = ""
    max_mp: float = Field(0.9, ge=0.2, le=2.0)


class CommitCharacterIn(BaseModel):
    name: str = ""
    image_id: str
    params: Dict[str, Any] = Field(default_factory=dict)


class CommitOutfitIn(BaseModel):
    name: str = ""
    slot: str = "full"
    image_id: str
    prompt: str = ""
    tags: List[str] = Field(default_factory=list)


class AdoptIn(BaseModel):
    image_id: str
    outfit_id: str = ""
    slot: str = ""
    note: str = ""


class RestoreIn(BaseModel):
    index: int = -1                # -1 = 回到素体，0 = 上一版


class RenameIn(BaseModel):
    name: str = ""


class PreviewIn(BaseModel):
    kind: str = "character"        # character | outfit | wear | tweak
    params: Dict[str, Any] = Field(default_factory=dict)


# ============================================================
#  选项目录 / 资料库
# ============================================================

@router.get("/options", summary="捏团子的可选项")
async def options():
    return svc.options_payload()


@router.get("/library", summary="资料库（团子 + 衣柜）")
async def library():
    return {
        "characters": store.list_characters(),
        "outfits": store.list_outfits(),
        "stats": store.stats(),
    }


@router.get("/library/characters/{cid}", summary="团子详情（含历史造型）")
async def character_detail(cid: str):
    c = store.get_character(cid)
    if not c:
        raise _not_found("团子")
    return c


@router.patch("/library/characters/{cid}", summary="改团子名字")
async def rename_character(cid: str, req: RenameIn):
    c = store.update_character(cid, name=req.name)
    if not c:
        raise _not_found("团子")
    return c


@router.delete("/library/characters/{cid}", summary="删除团子")
async def drop_character(cid: str):
    if not store.delete_character(cid):
        raise _not_found("团子")
    return {"ok": True}


@router.patch("/library/outfits/{fid}", summary="改衣服名字 / 槽位 / 标签")
async def edit_outfit(fid: str, req: dict):
    o = store.update_outfit(fid, **{k: req[k] for k in ("name", "slot", "tags") if k in req})
    if not o:
        raise _not_found("这件衣服")
    return o


@router.delete("/library/outfits/{fid}", summary="删除衣服")
async def drop_outfit(fid: str):
    if not store.delete_outfit(fid):
        raise _not_found("这件衣服")
    return {"ok": True}


# ============================================================
#  提示词预览（不入队，不花钱）
# ============================================================

@router.post("/preview", summary="只组装提示词，不出图")
async def preview(req: PreviewIn):
    try:
        if req.kind == "character":
            built = svc.build_character_prompt(req.params)
        elif req.kind == "outfit":
            built = svc.build_outfit_prompt(req.params)
        elif req.kind == "tweak":
            built = svc.build_tweak_prompt(str(req.params.get("tweak") or ""),
                                           str(req.params.get("value") or ""))
        elif req.kind == "wear":
            names = [str(n) for n in (req.params.get("outfit_names") or [])]
            built = svc.build_wear_prompt(
                slot=str(req.params.get("slot") or "full"),
                outfit_names=names or ["服装单品"],
                extra=str(req.params.get("extra") or ""),
                combo=len(names) > 1,
            )
        else:
            raise DangoError(f"未知的预览类型：{req.kind}")
    except DangoError as exc:
        raise _bad(exc)
    return {"kind": req.kind, "prompt": built["prompt"],
            "negative": built.get("negative", ""), "sections": built["sections"]}


# ============================================================
#  出图：捏团子 / 做衣服
# ============================================================

async def _submit(payload: dict, *, extra: Optional[dict] = None) -> dict:
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
    return {
        **created,
        "prompt": payload["prompt"],
        "negative": payload.get("negative_prompt", ""),
        "width": payload["width"],
        "height": payload["height"],
        "steps": payload["steps"],
        "mode": payload["mode"],
        "estimate": est,
        **(extra or {}),
    }


@router.post("/characters", summary="捏团子：出素体（文生图）")
async def create_character(req: CharacterIn):
    try:
        built = svc.payload_character(req.params, steps=req.steps, seed=req.seed,
                                      count=req.count, width=req.width, height=req.height)
    except DangoError as exc:
        raise _bad(exc)
    out = await _submit(built["payload"], extra={
        "kind": "character",
        "sections": built["info"]["sections"],
        "meta": built["info"]["meta"],
        "suggest_name": req.name or "",
    })
    return out


@router.post("/outfits/design", summary="做衣服：出单品图（文生图）")
async def design_outfit(req: OutfitDesignIn):
    params = {
        "description": req.description,
        "slot": req.slot,
        "style": req.style,
    }
    try:
        built = svc.payload_outfit(params, steps=req.steps, seed=req.seed,
                                   count=req.count, width=req.width, height=req.height)
    except DangoError as exc:
        raise _bad(exc)
    return await _submit(built["payload"], extra={
        "kind": "outfit",
        "sections": built["info"]["sections"],
        "meta": built["info"]["meta"],
        "suggest_name": req.name or req.description[:16],
    })


# ============================================================
#  出图：换装 / 微调（编辑通道）
# ============================================================

@router.post("/wear", summary="换装：多参考图编辑")
async def wear(req: WearIn):
    character = store.get_character(req.character_id)
    if not character:
        raise _not_found("团子")
    outfits = []
    for oid in req.outfit_ids[:3]:
        o = store.get_outfit(oid)
        if not o:
            raise _not_found(f"衣服 {oid}")
        outfits.append(o)
    if not outfits:
        raise _bad(DangoError("请先在衣柜里选一件衣服"))

    # 单件时以衣服自己的槽位为准（衣服知道自己该穿在哪），
    # 用户显式指定时以用户为准；多件时统一用 combo 的写法，槽位不参与提示词。
    slot = req.slot or "full"
    if len(outfits) == 1 and slot == "full":
        slot = str(outfits[0].get("slot") or "full")

    current = character.get("current") or character.get("base")
    try:
        built = svc.payload_wear(base_look=current, outfits=outfits, slot=slot,
                                 extra=req.extra, steps=req.steps, seed=req.seed,
                                 count=req.count, max_mp=req.max_mp)
    except DangoError as exc:
        raise _bad(exc)
    return await _submit(built["payload"], extra={
        "kind": "wear",
        "sections": built["info"]["sections"],
        "meta": built["info"]["meta"],
        "character_id": character["id"],
        "outfit_ids": [o["id"] for o in outfits],
        "slot": slot,
    })


@router.post("/tweak", summary="微调：换发色 / 换表情 / 衣服改色…（单图局部编辑）")
async def tweak(req: TweakIn):
    character = store.get_character(req.character_id)
    if not character:
        raise _not_found("团子")
    current = character.get("current") or character.get("base")
    try:
        built = svc.payload_tweak(base_look=current, tweak=req.tweak, value=req.value,
                                  steps=req.steps, seed=req.seed, count=req.count,
                                  max_mp=req.max_mp)
    except DangoError as exc:
        raise _bad(exc)
    return await _submit(built["payload"], extra={
        "kind": "tweak",
        "sections": built["info"]["sections"],
        "character_id": character["id"],
    })


# ============================================================
#  采纳：把某张出图变成资产
# ============================================================

def _look_from_image(image_id: str, **extra: Any) -> dict:
    image = repo.get_image(image_id)
    if not image:
        raise _not_found("这张图")
    return store.make_look(
        image_id=image["id"],
        url=image.get("url") or "",
        path=image.get("path") or "",
        width=image.get("width") or 0,
        height=image.get("height") or 0,
        seed=image.get("seed") if image.get("seed") is not None else -1,
        prompt=image.get("prompt") or "",
        **extra,
    )


@router.post("/characters/commit", summary="把一张出图保存为团子（素体）")
async def commit_character(req: CommitCharacterIn):
    look = _look_from_image(req.image_id)
    record = store.add_character(name=req.name, base=look, params=req.params)
    return record


@router.post("/outfits/commit", summary="把一张出图保存为衣柜单品")
async def commit_outfit(req: CommitOutfitIn):
    image = _look_from_image(req.image_id)
    record = store.add_outfit(name=req.name, slot=req.slot, image=image,
                              prompt=req.prompt)
    return record


@router.post("/characters/{cid}/adopt", summary="把一张出图设为团子的当前造型")
async def adopt(cid: str, req: AdoptIn):
    if not store.get_character(cid):
        raise _not_found("团子")
    outfit_name = ""
    if req.outfit_id:
        o = store.get_outfit(req.outfit_id)
        outfit_name = (o or {}).get("name") or ""
    look = _look_from_image(req.image_id, outfit_id=req.outfit_id,
                            outfit_name=outfit_name, slot=req.slot, note=req.note)
    record = store.adopt_look(cid, look)
    if not record:
        raise _not_found("团子")
    return record


@router.post("/characters/{cid}/restore", summary="回到上一版造型 / 回到素体")
async def restore(cid: str, req: RestoreIn):
    record = store.restore_look(cid, req.index)
    if not record:
        raise _bad(DangoError("没有可回退的造型"))
    return record


# ============================================================
#  任务
# ============================================================

@router.get("/jobs/{job_id}", summary="任务详情（含出图）")
async def job_detail(job_id: str):
    job = repo.get_job(job_id)
    if not job:
        raise _not_found("任务")
    job = dict(job)
    job["images"] = repo.images_by_job(job_id)
    return job


@router.post("/jobs/{job_id}/cancel", summary="中断当前推理")
async def job_cancel(job_id: str):
    job = repo.get_job(job_id)
    if not job:
        raise _not_found("任务")
    try:
        engine.cancel_current()
    except Exception as exc:
        raise _bad(exc)
    return {"ok": True, "message": "已请求中断当前推理"}


# ============================================================
#  独立运行入口
# ============================================================

def create_app():
    """独立运行：
        uvicorn apps.dango.backend.routes:create_app --factory --port 8104
    """
    from pathlib import Path as _Path

    from fastapi import FastAPI

    from shared.webapp import mount_frontend, mount_media

    application = FastAPI(title="团子衣橱", version="1.0.0")
    application.include_router(router, prefix="/api/v1")
    mount_media(application)
    frontend = _Path(__file__).resolve().parents[1] / "frontend"
    mount_frontend(application, frontend, at="/", name="dango")
    return application
