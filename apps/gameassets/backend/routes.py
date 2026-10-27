# -*- coding: utf-8 -*-
"""游戏素材工坊接口 5.0 —— 分类独立生成 + 异常透明 + 可控精修 + 导出。

    POST /generate                      一句话建项目 + 全量编排（plan→master→六类）
    POST /sets/{sid}/stages/{kind}/run  单独生成某类（支持筛选：failed/tone/season）
    POST /sets/{sid}/stages/{kind}/cancel  取消运行中/排队的阶段
    GET  /sets/{sid}                    详情：stages 各类状态/进度/错误 + 日志 + alive
    POST /sets/{sid}/assets/{aid}/rerun    单卡重跑（质量守门 + 透明底 + 版本历史）
    POST /sets/{sid}/assets/{aid}/variants 单张多候选（改提示词/锁 seed/N 张挑选）
    POST /sets/{sid}/assets/{aid}/commit   候选提交为正式版（旧版进历史）
    POST /sets/{sid}/assets/{aid}/rollback 版本回滚
    PATCH /sets/{sid}/plan              方案可视化编辑（增删改条目，只重跑受影响项）
    PATCH /sets/{sid}/settings          透明底方式 / 画幅 / 阈值 按项目覆盖
    GET  /sets/{sid}/export             一键导出 ZIP（可带引擎导入清单）
    GET  /sets/{sid}/atlas/{kind}       图集拼版（sprite sheet）
    GET  /sets/{sid}/contact-sheet      全项目接触表（棋盘格审阅图，11.0）
    PATCH /sets/{sid}/settings          透明底方式 / 画幅 / 阈值 / 美术方向契约 按项目覆盖
    POST /sets/{sid}/simulate           仿真测试：逐张体检 + 合成模拟游戏画面

异常原则：任何阶段失败都落库并展示（状态 chip + 日志行），前端还有
alive 标志与服务断线检测——绝不出现"页面还在转但其实已经死了"。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import anim, artdir, exporter, pipeline, store, tonecheck
from .planner import SCALES
from .presets import (ASSET_KINDS, SEASONS, SEASON_ORDER, TIER_ORDER,
                      WEAPON_TIERS, TIME_PRESETS, GENDERS,
                      MONSTER_TIERS, MONSTER_TIER_ORDER,
                      SCENE_USAGES, SCENE_USAGE_ORDER, transparent_mode,
                      ANIM_LEVELS, ANIM_LEVEL_ORDER, ANIM_KINDS, ANIM_DEFAULT_LEVEL,
                      VIEW_LOCK, DIRECTIONS, CLIPS, anim_spec, anim_frames_of)
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


class RunIn(BaseModel):
    """筛选重跑：只跑命中条目（不传 = 全量重跑该类）。"""
    filter: str | None = Field(
        None, description="failed | tone | season | gender | tier | zone")
    season: str | None = Field(None, description="filter=season 时的季节 id")
    gender: str | None = Field(None, description="filter=gender 时的性别 id：male | female")
    tier: str | None = Field(None, description="filter=tier 时的怪物等级 id")
    zone: str | None = Field(None, description="filter=zone 时的场景名（只跑该场景的怪）")


class VariantsIn(BaseModel):
    prompt: str | None = Field(None, max_length=2000, description="改写后的提示词（不传用原词）")
    seed: int = Field(-1, description="锁定种子（-1 随机）")
    n: int = Field(1, ge=1, le=4, description="候选张数")
    use_self_ref: bool = Field(True, description="带自身参考图（多姿态一致性）")


class CommitIn(BaseModel):
    variant: dict = Field(..., description="variants 返回的候选对象")


class RollbackIn(BaseModel):
    index: int = Field(..., ge=0, le=4, description="历史版本下标（0=最近一版）")


class PlanOp(BaseModel):
    action: str = Field(..., description="update | delete | add")
    index: int | None = Field(None, ge=0, description="方案条目下标（update/delete 必填）")
    name: str | None = Field(None, max_length=40)
    desc: str | None = Field(None, max_length=200)
    tier_id: str | None = Field(None, description="等级：武器 common/fine/epic/legendary；"
                                                  "怪物 minion/elite/boss/worldboss")
    usage: str | None = Field(None, description="场景用途（仅 scene）：battle|tile|"
                                                "portrait|banner|iso，决定画幅")
    scene: str | None = Field(None, description="怪物出没的场景名（仅 monster）")
    seasons: list[str] | None = Field(None, description="场景季列表（仅 scene，缺省四季）")
    genders: list[str] | None = Field(None, description="角色性别列表（仅 character：male/female）")
    anim: str | None = Field(None, description="动画档位（角色/宠物）：off|lite|standard|full，"
                                              "决定朝向数与站立/行走帧数")
    tileable: bool | None = Field(None, description="兼容旧字段：等价于 usage=tile")


class PlanEditIn(BaseModel):
    kind: str
    ops: list[PlanOp] = Field(..., min_length=1)


class SettingsIn(BaseModel):
    transparent: dict[str, str] | None = Field(
        None, description="按类抠图方式：none|chroma|rembg")
    sizes: dict[str, list[int]] | None = Field(None, description="按类画幅覆盖 [w,h]")
    threshold: float | None = Field(None, ge=10, le=120)
    prompts: dict[str, dict] | None = Field(
        None, description='按类提示词补充：{"character": {"extra": "...", "negative": "..."}}')
    anim_level: str | None = Field(
        None, description="项目默认动画档位：off|lite|standard|full（角色/宠物）")
    artdir: dict[str, str] | None = Field(
        None, description="美术方向契约（八轴，见 /presets 的 artdir_axes）；传空对象恢复自动推导")


@router.get("/presets", summary="风格 / 时间背景 / 规模 / 季节 / 性别 / 等级 / 场景用途 预设")
async def list_presets():
    from .presets import (ASPECTS, TRANSPARENT_DEFAULT, CLASS_LABELS,
                          CLASS_TEMPLATE_HINT, GENDER_ORDER)
    return {
        "styles": list(STYLE_PRESETS.values()),
        "times": list(TIME_PRESETS.values()),
        "scales": [{"id": k, "name": v["name"],
                    "scenes": v["scenes"], "characters": v["characters"],
                    "pets": v["pets"], "weapons": v["weapons"],
                    "buildings": v["buildings"], "monsters": v["monsters"],
                    # 主角默认男女各一版，按 +1 张估（不是每个角色都双版）
                    "images": v["scenes"] * 4 + v["characters"] + 1 + v["pets"]
                              + v["weapons"] + v["buildings"] + v["monsters"] + 1}
                   for k, v in SCALES.items()],
        "seasons": [{"id": k, "name": SEASONS[k]["name"]} for k in SEASON_ORDER],
        "tiers": [{"id": k, "name": WEAPON_TIERS[k]["name"]} for k in TIER_ORDER],
        "monster_tiers": [{"id": k, "name": MONSTER_TIERS[k]["name"],
                           "size": list(MONSTER_TIERS[k]["size"]),
                           "note": MONSTER_TIERS[k]["note"]}
                          for k in MONSTER_TIER_ORDER],
        "scene_usages": [{"id": k, "name": SCENE_USAGES[k]["name"],
                          "size": list(SCENE_USAGES[k]["size"]),
                          "aspect": SCENE_USAGES[k]["aspect"],
                          "reason": SCENE_USAGES[k]["reason"]}
                         for k in SCENE_USAGE_ORDER],
        "stages": [{"id": k, "name": v} for k, v in store.STAGES],
        "aspects": {k: list(v) for k, v in ASPECTS.items()},
        "transparent_default": TRANSPARENT_DEFAULT,
        "genders": [{"id": k, "name": GENDERS[k]["name"]} for k in GENDER_ORDER],
        "class_labels": CLASS_LABELS,
        "class_templates": CLASS_TEMPLATE_HINT,
        # 2.5D 斜 45° 视图锁 + 动画档位（前端方案编辑器据此出下拉与张数预估）
        "view": {"id": VIEW_LOCK["id"], "name": VIEW_LOCK["name"],
                 "cell": list(VIEW_LOCK["cell"]), "anchor": list(VIEW_LOCK["anchor"])},
        "anim_levels": [{"id": k, "name": ANIM_LEVELS[k]["name"],
                         "dirs": ANIM_LEVELS[k]["dirs"],
                         "idle": ANIM_LEVELS[k]["idle"], "walk": ANIM_LEVELS[k]["walk"],
                         "frames": anim_spec(k)["frames_total"],
                         "note": ANIM_LEVELS[k]["note"]}
                        for k in ANIM_LEVEL_ORDER],
        "anim_kinds": list(ANIM_KINDS),
        "anim_default": ANIM_DEFAULT_LEVEL,
        "anim_dirs": [{"id": k, "name": DIRECTIONS[k]["name"],
                       "short": DIRECTIONS[k]["short"]} for k in DIRECTIONS],
        "clips": [{"id": k, "name": CLIPS[k]["name"], "fps": CLIPS[k]["fps"],
                   "frames": len(CLIPS[k]["poses"])} for k in CLIPS],
        # 美术方向契约八轴定义（前端设置面板据此渲染编辑区）
        "artdir_axes": [{"id": k, "name": v["name"], "hint": v["hint"]}
                        for k, v in artdir.AXES.items()],
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


def _enqueue_or_409(sid: str, kind: str, opts: dict | None = None) -> None:
    try:
        pipeline.enqueue(sid, kind, opts)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "BUSY" if "已在队列" in str(exc) else "NOT_READY",
            "message": str(exc)})


@router.post("/sets/{sid}/stages/{kind}/run",
             summary="单独生成/重试一个阶段（body 可选筛选：failed/tone/season）")
async def run_stage(sid: str, kind: str, body: RunIn | None = None):
    if not store.get_set(sid):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "项目不存在"})
    if kind not in _RUNNABLE:
        raise HTTPException(status_code=400, detail={
            "code": "BAD_STAGE", "message": f"未知阶段：{kind}（可选：{', '.join(sorted(_RUNNABLE))}）"})
    opts = {}
    if body and body.filter:
        if kind not in ASSET_KINDS:
            raise HTTPException(status_code=400, detail={
                "code": "BAD_FILTER", "message": "筛选重跑只对六类素材阶段有效"})
        if body.filter not in ("failed", "tone", "season", "gender", "tier", "zone"):
            raise HTTPException(status_code=400, detail={
                "code": "BAD_FILTER",
                "message": "filter 可选：failed | tone | season | gender | tier | zone"})
        if body.filter == "season":
            if body.season not in SEASONS:
                raise HTTPException(status_code=400, detail={
                    "code": "BAD_SEASON", "message": f"未知季节：{body.season}"})
            opts = {"filter": "season", "season": body.season}
        elif body.filter == "gender":
            if body.gender not in GENDERS:
                raise HTTPException(status_code=400, detail={
                    "code": "BAD_GENDER", "message": f"未知性别：{body.gender}"})
            opts = {"filter": "gender", "gender": body.gender}
        elif body.filter == "tier":
            if body.tier not in MONSTER_TIERS:
                raise HTTPException(status_code=400, detail={
                    "code": "BAD_TIER", "message": f"未知怪物等级：{body.tier}"})
            opts = {"filter": "tier", "tier": body.tier}
        elif body.filter == "zone":
            if not str(body.zone or "").strip():
                raise HTTPException(status_code=400, detail={
                    "code": "BAD_ZONE", "message": "按场景筛选需要 zone（场景名）"})
            opts = {"filter": "zone", "zone": body.zone.strip()}
        else:
            opts = {"filter": body.filter}
    _enqueue_or_409(sid, kind, opts)
    return {"ok": True}


@router.post("/sets/{sid}/stages/{kind}/cancel", summary="取消运行中/排队的阶段")
async def cancel_stage(sid: str, kind: str):
    if not store.get_set(sid):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "项目不存在"})
    if kind != "all" and kind not in store.STAGE_IDS:
        raise HTTPException(status_code=400, detail={"code": "BAD_STAGE", "message": "未知阶段"})
    r = pipeline.cancel_stage(sid)
    if kind == "all":
        return r
    # 单类取消：只关心该类
    return {"ok": True, **r}


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


@router.post("/sets/{sid}/assets/{aid}/rerun", summary="重跑单张素材（守门 + 透明底 + 旧版进历史）")
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
    old = next((a for a in s["assets"] if a["id"] == aid), None)
    if not old:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "素材不存在"})

    kind = old["kind"]
    item = {"id": old.get("item_id") or pipeline.new_id("itm"),
            "kind": kind, "name": old["name"], "desc": old["desc"],
            "season": old.get("season"), "tier_id": old.get("tier_id"),
            "gender": old.get("gender"), "tileable": old.get("tileable"),
            "usage": old.get("usage"), "scene": old.get("scene")}
    threshold = float(s.get("threshold") or tonecheck.DEFAULT_THRESHOLD) * (
        pipeline._SEASON_THRESHOLD_FACTOR if old.get("season") else 1.0)
    try:
        asset = await pipeline._gen_with_gate(
            sid, kind=kind, item=item, prompt=pipeline.asset_prompt(s, item),
            negative=pipeline.asset_negative(s, kind), threshold=threshold,
            size=tuple(old.get("size") or pipeline.asset_size(s, kind, item)),
            tmode=pipeline.transparent_mode(s, kind), master_path=master["path"])
    except Exception as exc:
        store.append_log(sid, "error", f"重跑「{old['name']}」失败：{exc}")
        raise HTTPException(status_code=500, detail={"code": "GEN_FAILED", "message": str(exc)[:200]})
    pipeline._upsert_asset(sid, asset)
    store.append_log(sid, "info",
                     f"重跑「{old['name']}」完成 色调{asset['tone']['score']:.0f}（旧版进历史）")
    return asset


@router.post("/sets/{sid}/assets/{aid}/variants",
             summary="单张多候选：改提示词 / 锁 seed / 出 N 张挑选（不改正式版）")
async def make_variants(sid: str, aid: str, body: VariantsIn):
    if not store.get_set(sid):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "项目不存在"})
    if pipeline.is_busy(sid):
        raise HTTPException(status_code=409, detail={"code": "BUSY", "message": "项目有任务在队列中，稍后再试"})
    try:
        variants = await pipeline.gen_variants(
            sid=sid, aid=aid, prompt=body.prompt, seed=body.seed,
            n=body.n, use_self_ref=body.use_self_ref)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "BAD_REQUEST", "message": str(exc)})
    except Exception as exc:
        store.append_log(sid, "error", f"候选生成失败：{exc}")
        raise HTTPException(status_code=500, detail={"code": "GEN_FAILED", "message": str(exc)[:200]})
    return {"variants": variants}


@router.post("/sets/{sid}/assets/{aid}/commit", summary="候选提交为正式版（旧版进历史）")
async def commit_variant(sid: str, aid: str, body: CommitIn):
    try:
        asset = pipeline.commit_variant(sid=sid, aid=aid, variant=body.variant)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "BAD_REQUEST", "message": str(exc)})
    return asset


@router.post("/sets/{sid}/assets/{aid}/rollback", summary="回滚到历史版本")
async def rollback_asset(sid: str, aid: str, body: RollbackIn):
    result = {"ok": False}

    def _apply(t: dict) -> None:
        idx = next((i for i, a in enumerate(t["assets"]) if a["id"] == aid), None)
        if idx is None:
            return
        a = t["assets"][idx]
        hist = a.get("history") or []
        if body.index >= len(hist):
            return
        old_ver = hist.pop(body.index)
        hist.insert(0, {k: a.get(k) for k in
                        ("path", "url", "seed", "tone", "created_at",
                         "prompt", "transparent")})
        del hist[pipeline._MAX_HISTORY:]
        t["assets"][idx] = {**a, **old_ver, "history": hist}
        result["ok"] = True
    store.mutate(sid, _apply)
    if not result["ok"]:
        raise HTTPException(status_code=404, detail={
            "code": "NOT_FOUND", "message": "素材或历史版本不存在"})
    store.append_log(sid, "info", "素材已回滚到历史版本")
    s = store.get_set(sid)
    return next(a for a in s["assets"] if a["id"] == aid)


@router.patch("/sets/{sid}/plan", summary="方案可视化编辑（增删改条目，只重跑受影响项）")
async def edit_plan(sid: str, body: PlanEditIn):
    s = store.get_set(sid)
    if not s:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "项目不存在"})
    if not s.get("plan"):
        raise HTTPException(status_code=409, detail={"code": "NO_PLAN", "message": "还没有策划方案"})
    if body.kind not in ASSET_KINDS:
        raise HTTPException(status_code=400, detail={"code": "BAD_KIND", "message": "未知素材类别"})
    if pipeline.is_busy(sid):
        raise HTTPException(status_code=409, detail={"code": "BUSY", "message": "项目有任务在队列中，先取消或等跑完"})
    try:
        changed = _apply_plan_edit(sid, s, body.kind, body.ops)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "BAD_OP", "message": str(exc)})
    return {"ok": True, "changed": changed}


def _scene_usage_fields(op) -> dict:
    """场景用途 → 用途 id（+ 兼容字段 tileable）。用途决定画幅，见 presets.SCENE_USAGES。"""
    if op.usage is not None and op.usage not in SCENE_USAGES:
        raise ValueError(f"未知场景用途：{op.usage}"
                         f"（可选：{', '.join(SCENE_USAGE_ORDER)}）")
    uid = op.usage if op.usage in SCENE_USAGES else ("tile" if op.tileable else "battle")
    return {"usage": uid, "tileable": uid == "tile"}


def _anim_payload(am: dict) -> dict:
    """落库的 anim 元数据 → 运行时/前端要的紧凑结构（图集 URL + 行号）。"""
    banks = {}
    for dir_id, b in (am.get("banks") or {}).items():
        rows = {cid: (b.get("clips") or {}).get(cid, {}).get("row", i)
                for i, cid in enumerate(("idle", "walk"))}
        banks[dir_id] = {"url": b.get("url"), "cols": b.get("cols"), "rows": rows}
    return {"cell": am.get("cell"), "anchor": am.get("anchor"),
            "dirs": am.get("dirs") or [], "mirror": am.get("mirror") or {},
            "level": am.get("level"), "level_name": am.get("level_name"),
            "frames_total": am.get("frames_total"),
            "clips": {cid: {"name": c.get("name"), "fps": c.get("fps"),
                            "count": c.get("count")}
                      for cid, c in (am.get("clips") or {}).items()},
            "banks": banks}


def _anim_fields(op) -> dict:
    """动画档位（角色/宠物）。空字符串 = 跟项目默认档。"""
    if op.anim is None:
        return {}
    lv = str(op.anim).strip()
    if lv and lv not in ANIM_LEVELS:
        raise ValueError(f"未知动画档位：{lv}（可选：{', '.join(ANIM_LEVEL_ORDER)}）")
    return {"anim_level": lv}


def _monster_fields(op, s: dict, base: dict | None = None) -> dict:
    """怪物等级 + 出没场景。场景必须是本项目已有场景名（配属关系不能悬空）。"""
    base = base or {}
    tid = op.tier_id if op.tier_id is not None else base.get("tier_id", "minion")
    if tid not in MONSTER_TIERS:
        raise ValueError(f"未知怪物等级：{tid}"
                         f"（可选：{', '.join(MONSTER_TIER_ORDER)}）")
    names = [sc["name"] for sc in (s.get("plan", {}).get("scenes") or [])]
    want = op.scene if op.scene is not None else base.get("scene")
    want = str(want or "").strip()
    if want and want not in names:
        raise ValueError(f"场景不存在：{want}（可选：{'、'.join(names) or '无'}）")
    return {"tier_id": tid, "tier": MONSTER_TIERS[tid]["name"],
            "scene": want or (names[0] if names else None)}


def _apply_plan_edit(sid: str, s: dict, kind: str, ops: list) -> dict:
    """改方案条目 + 重建该类条目表。改动/新增的条目重置 pending，未动的原样保留。"""
    from .presets import PLAN_KEYS, build_stage_items, GENDERS
    from shared.models_util import new_id
    key = PLAN_KEYS[kind]
    entries = list(s["plan"][key])
    changed = {"updated": 0, "added": 0, "deleted": 0}

    for op in ops:
        if op.action == "add":
            entry = {"name": (op.name or "新条目").strip(),
                     "desc": (op.desc or "").strip()}
            if kind == "weapon":
                tier = next((t for t in TIER_ORDER if t == op.tier_id), "common")
                entry["tier"] = WEAPON_TIERS[tier]["name"]
                entry["tier_id"] = tier
            if kind == "scene":
                entry["seasons"] = [x for x in (op.seasons or SEASON_ORDER)
                                    if x in SEASONS] or SEASON_ORDER
                entry.update(_scene_usage_fields(op))
            if kind == "character":
                g = [x for x in (op.genders or ["male"]) if x in GENDERS]
                entry["genders"] = g or ["male"]
            if kind in ANIM_KINDS:
                entry.update(_anim_fields(op))
            if kind == "monster":
                entry.update(_monster_fields(op, s))
            entries.append(entry)
            changed["added"] += 1
        elif op.action == "delete":
            if op.index is None or op.index >= len(entries):
                raise ValueError(f"删除下标越界：{op.index}")
            entries.pop(op.index)
            changed["deleted"] += 1
        elif op.action == "update":
            if op.index is None or op.index >= len(entries):
                raise ValueError(f"更新下标越界：{op.index}")
            e = dict(entries[op.index])
            if op.name is not None:
                e["name"] = op.name.strip()
            if op.desc is not None:
                e["desc"] = op.desc.strip()
            if kind == "weapon" and op.tier_id:
                if op.tier_id not in TIER_ORDER:
                    raise ValueError(f"未知武器等级：{op.tier_id}")
                e["tier"] = WEAPON_TIERS[op.tier_id]["name"]
                e["tier_id"] = op.tier_id
            if kind == "scene":
                if op.seasons is not None:
                    e["seasons"] = [x for x in op.seasons if x in SEASONS] or SEASON_ORDER
                if op.usage is not None or op.tileable is not None:
                    e.update(_scene_usage_fields(op))
            if kind == "character" and op.genders is not None:
                g = [x for x in op.genders if x in GENDERS]
                e["genders"] = g or ["male"]
            if kind in ANIM_KINDS and op.anim is not None:
                e.update(_anim_fields(op))
            if kind == "monster" and (op.tier_id is not None or op.scene is not None):
                e.update(_monster_fields(op, s, base=e))
            entries[op.index] = e
            changed["updated"] += 1
        else:
            raise ValueError(f"未知操作：{op.action}")

    def _apply(t: dict) -> None:
        old_names = {it["name"] for it in t["pipeline"]["stages"][kind]["items"]}
        t["plan"][key] = entries
        blueprint = build_stage_items(
            t["plan"], {k: t["pipeline"]["stages"][k]["items"] for k in ASSET_KINDS})
        new_items = blueprint[kind]
        old_items_by_id = {it.get("id"): it
                           for it in t["pipeline"]["stages"][kind]["items"]}
        # 对账：内容没变的条目沿用状态；变了的置 pending 并清掉旧素材
        for it in new_items:
            prev = old_items_by_id.get(it.get("id"))
            if prev and prev["name"] == it["name"] and prev["desc"] == it["desc"] \
                    and prev.get("tier_id") == it.get("tier_id") \
                    and prev.get("gender") == it.get("gender") \
                    and prev.get("usage") == it.get("usage") \
                    and prev.get("scene") == it.get("scene") \
                    and prev.get("anim_level") == it.get("anim_level"):
                it["status"] = prev["status"]
                it["score"] = prev.get("score")
        removed = old_names - {it["name"] for it in new_items}
        t["pipeline"]["stages"][kind]["items"] = new_items
        t["pipeline"]["stages"][kind]["total"] = len(new_items)
        done = sum(1 for it in new_items if it["status"] == "done")
        t["pipeline"]["stages"][kind]["done"] = done
        # 清素材：只保留「条目未动（仍 done/failed）」的旧素材；
        # 被删除/内容改动（置 pending）的条目，其旧素材一并清掉
        kept = {(it["name"], it.get("season"), it.get("tier_id"), it.get("gender"))
                for it in new_items if it["status"] in ("done", "failed")}
        t["assets"] = [
            a for a in t["assets"]
            if a["kind"] != kind
            or (a["name"], a.get("season"), a.get("tier_id"), a.get("gender")) in kept]
    store.mutate(sid, _apply)
    store.append_log(
        sid, "info",
        f"方案已编辑（{store.STAGE_LABELS[kind]}）：改 {changed['updated']} / "
        f"增 {changed['added']} / 删 {changed['deleted']}，受影响条目待重跑")
    return changed


@router.patch("/sets/{sid}/settings",
              summary="按项目覆盖设置（透明底 / 画幅 / 色调阈值 / 按类提示词）")
async def edit_settings(sid: str, body: SettingsIn):
    if not store.get_set(sid):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "项目不存在"})

    def _apply(t: dict) -> None:
        if body.transparent is not None:
            bad = {k: v for k, v in body.transparent.items()
                   if k not in ASSET_KINDS or v not in ("none", "chroma", "rembg")}
            if bad:
                raise ValueError(f"非法透明底设置：{bad}")
            t["transparent"] = {**(t.get("transparent") or {}), **body.transparent}
        if body.sizes is not None:
            bad = {k: v for k, v in body.sizes.items()
                   if k not in ASSET_KINDS or len(v) != 2
                   or not (256 <= v[0] <= 1536 and 256 <= v[1] <= 1536)}
            if bad:
                raise ValueError(f"非法画幅设置（宽高 256~1536）：{bad}")
            t["sizes"] = {**(t.get("sizes") or {}), **body.sizes}
        if body.threshold is not None:
            t["threshold"] = body.threshold
        if body.anim_level is not None:
            if body.anim_level not in ANIM_LEVELS:
                raise ValueError(f"未知动画档位：{body.anim_level}"
                                 f"（可选：{', '.join(ANIM_LEVEL_ORDER)}）")
            t["anim"] = {**(t.get("anim") or {}), "anim_level": body.anim_level}
        if body.prompts is not None:
            from .presets import CLASS_LABELS
            bad = set(body.prompts) - set(ASSET_KINDS)
            if bad:
                raise ValueError(f"非法分类提示词：{sorted(bad)}（可选：{sorted(CLASS_LABELS)}）")
            merged = {**(t.get("prompts") or {})}
            for k, v in body.prompts.items():
                if not isinstance(v, dict):
                    raise ValueError(f"{k} 的提示词必须是 {{extra, negative}} 对象")
                extra = str(v.get("extra") or "").strip()[:600]
                negative = str(v.get("negative") or "").strip()[:400]
                if not extra and not negative:
                    merged.pop(k, None)          # 清空 = 恢复默认
                else:
                    merged[k] = {"extra": extra, "negative": negative}
            t["prompts"] = merged
        if body.artdir is not None:
            from . import artdir as _artdir
            cleaned = _artdir.normalize(body.artdir)
            t["artdir"] = cleaned or _artdir.derive(t)   # 清空 = 回到自动推导
            t["artdir_edited"] = bool(cleaned)
    try:
        store.mutate(sid, _apply)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "BAD_SETTINGS", "message": str(exc)})
    return store.get_set(sid)


@router.get("/sets/{sid}/export", summary="一键导出 ZIP（engine=unity|godot|phaser 可带导入清单）")
async def export_set(sid: str, engine: str | None = None):
    s = store.get_set(sid)
    if not s:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "项目不存在"})
    if not s["assets"]:
        raise HTTPException(status_code=409, detail={"code": "EMPTY", "message": "还没有素材可导出"})
    zip_path = exporter.build_zip(s, engine_tpl=engine)
    store.append_log(sid, "info",
                     f"已导出 ZIP（{len(s['assets'])} 张"
                     + (f"，{engine} 清单" if engine else "") + "）")
    return {"url": exporter.export_url(zip_path),
            "filename": zip_path.name, "count": len(s["assets"])}


@router.get("/sets/{sid}/atlas/{kind}", summary="图集拼版（sprite sheet，cols 每行列数）")
async def atlas(sid: str, kind: str, cols: int = 4):
    s = store.get_set(sid)
    if not s:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "项目不存在"})
    if kind not in ASSET_KINDS:
        raise HTTPException(status_code=400, detail={"code": "BAD_KIND", "message": "未知素材类别"})
    try:
        png = exporter.build_atlas(s, kind, cols=cols)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "EMPTY", "message": str(exc)})
    return {"url": exporter.export_url(png)}


@router.get("/sets/{sid}/contact-sheet",
            summary="全项目接触表（棋盘格审阅图：剪影/色板/烤死背景一眼看穿）")
async def contact_sheet(sid: str, cell: int = 192, cols: int = 4):
    s = store.get_set(sid)
    if not s:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "项目不存在"})
    try:
        png = await asyncio.to_thread(
            exporter.build_contact_sheet, s,
            max(64, min(512, cell)), max(1, min(8, cols)))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "EMPTY", "message": str(exc)})
    store.append_log(sid, "info", "已生成全项目接触表（棋盘格审阅图）")
    return {"url": exporter.export_url(png)}


@router.post("/sets/{sid}/simulate", summary="仿真测试：逐张体检 + 按场景合成模拟游戏画面（同步，秒级）")
async def simulate_set(sid: str):
    from . import simulator
    s = store.get_set(sid)
    if not s:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "项目不存在"})
    if not s["assets"]:
        raise HTTPException(status_code=409, detail={"code": "EMPTY", "message": "还没有素材，先跑完某类生成"})
    try:
        report = await asyncio.to_thread(simulator.run, s)
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"code": "SIM_FAIL", "message": str(exc)})
    n = report["summary"]
    store.append_log(sid, "info",
                     f"仿真测试完成：{n['groups']} 组画面，{n['pass']}/{n['total']} 张体检通过"
                     + (f"，{n['warn']} 组有提示" if n["warn"] else ""))
    return report


@router.post("/sets/{sid}/assets/{aid}/recut", summary="重抠透明底（rembg 优先，旧图进历史）")
async def recut_asset(sid: str, aid: str):
    s = store.get_set(sid)
    if not s:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "项目不存在"})
    asset = next((a for a in s["assets"] if a["id"] == aid), None)
    if not asset:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "素材不存在"})
    if transparent_mode(s, asset["kind"]) == "none":
        raise HTTPException(status_code=409, detail={
            "code": "NO_CUT", "message": "该类素材不做透明底（场景/建筑）"})
    try:
        updated = await asyncio.to_thread(pipeline.recut_asset, s, asset)
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"code": "CUT_FAIL", "message": str(exc)})
    rep = updated.get("transparent") or {}
    store.append_log(sid, "info",
                     f"重抠「{updated['name']}」：{rep.get('method')} "
                     f"透明{rep.get('alpha_ratio')}"
                     + ("（回落色键）" if rep.get("fallback") else ""))
    return updated


class RecutIn(BaseModel):
    kinds: list[str] | None = Field(None, description="只重抠这些类（缺省=全部该透明的类）")


@router.post("/sets/{sid}/recut", summary="批量重抠透明底（存量素材一次性救回来）")
async def recut_set(sid: str, body: RecutIn | None = None):
    s = store.get_set(sid)
    if not s:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "项目不存在"})
    kinds = (body.kinds if body and body.kinds else
             [k for k in ASSET_KINDS if transparent_mode(s, k) != "none"])
    targets = [a for a in s["assets"] if a["kind"] in kinds]
    if not targets:
        raise HTTPException(status_code=409, detail={"code": "EMPTY", "message": "没有可重抠的素材"})
    done, failed, fell_back = 0, 0, 0
    for a in targets:
        try:
            cur = store.get_set(sid)
            asset = next((x for x in cur["assets"] if x["id"] == a["id"]), None)
            if not asset:
                continue
            updated = await asyncio.to_thread(pipeline.recut_asset, cur, asset)
            done += 1
            rep = updated.get("transparent") or {}
            fell_back += 1 if rep.get("fallback") else 0
        except Exception as exc:
            failed += 1
            store.append_log(sid, "error", f"重抠「{a['name']}」失败：{exc}")
    store.append_log(sid, "info",
                     f"批量重抠完成：{done}/{len(targets)} 张"
                     + (f"，{fell_back} 张回落色键" if fell_back else "")
                     + (f"，{failed} 张失败" if failed else ""))
    return {"total": len(targets), "done": done, "failed": failed,
            "fallback": fell_back}


class PlaytestReportIn(BaseModel):
    group: int = Field(0, ge=0)
    result: dict = Field(..., description="运行时实测指标（前端机器人跑完回传）")


@router.get("/sets/{sid}/playtest", summary="实测运行时清单：场景+walk 掩码+精灵（缺透明底自动补抠）")
async def playtest_manifest(sid: str, group: int = 0):
    s = store.get_set(sid)
    if not s:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "项目不存在"})
    scenes = sorted([a for a in s["assets"] if a["kind"] == "scene"],
                    key=lambda a: a.get("season") or "")
    if not scenes:
        raise HTTPException(status_code=409, detail={"code": "NO_SCENE", "message": "还没有场景素材"})
    group = max(0, min(group, len(scenes) - 1))
    sc = scenes[group]

    # 场景可通行区域（带缓存），同步落记录
    from . import scene_analysis
    walk = None
    try:
        rep = scene_analysis.analyze_asset(sc)
        walk = {k: rep[k] for k in
                ("mask_url", "overlay_url", "mask_size", "mode", "horizon",
                 "walk_ratio", "combat_band", "spawns", "obstacles") if k in rep}

        def _apply(t: dict) -> None:
            for a in t["assets"]:
                if a["id"] == sc["id"]:
                    a["walk"] = walk
                    break
        store.mutate(sid, _apply)
    except Exception:
        walk = None

    async def sprite(a: dict) -> dict:
        """精灵：动画素材直接带图集信息；静态素材缺透明底就现场补抠（rembg）。"""
        out = {"id": a["id"], "name": a["name"], "url": a["url"],
               "gender": a.get("gender_name"), "tier": a.get("tier"),
               "tier_id": a.get("tier_id"), "alpha": False}
        if a.get("anim"):
            out.update({"alpha": True, "anim": _anim_payload(a["anim"])})
            return out
        if transparent_mode(s, a["kind"]) == "none":
            return out
        if "_alpha" not in Path(a["path"]).stem:
            try:
                cur = store.get_set(sid)
                cur_a = next(x for x in cur["assets"] if x["id"] == a["id"])
                a = await asyncio.to_thread(pipeline.recut_asset, cur, cur_a)
            except Exception:
                pass
        out["url"] = a["url"]
        out["alpha"] = "_alpha" in Path(a["path"]).stem
        return out

    chars = [a for a in s["assets"] if a["kind"] == "character"]
    pets = [a for a in s["assets"] if a["kind"] == "pet"]
    weapons = [a for a in s["assets"] if a["kind"] == "weapon"]
    monsters = ([m for m in s["assets"] if m["kind"] == "monster"
                 and m.get("scene") == sc["name"]]
                + ([m for m in s["assets"] if m["kind"] == "monster"
                    and not m.get("scene")] if group == 0 else []))

    return {
        "sid": sid, "group": group, "groups": len(scenes),
        "scene": {"name": sc["name"],
                  "season": sc.get("season_name") or "",
                  "usage": sc.get("usage_name") or "",
                  "url": sc["url"], "walk": walk},
        "player": await sprite(chars[0]) if chars else None,
        "pet": await sprite(pets[0]) if pets else None,
        "weapons": [await sprite(w) for w in weapons],
        "monsters": [await sprite(m) for m in monsters],
    }


@router.post("/sets/{sid}/playtest", summary="回传运行时实测报告（落库 + 日志）")
async def playtest_report(sid: str, body: PlaytestReportIn):
    s = store.get_set(sid)
    if not s:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "项目不存在"})
    report = {"created_at": store.now_iso(), "group": body.group, **body.result}

    def _apply(t: dict) -> None:
        pt = t.setdefault("playtest", {})
        pt.update(report)
        log = t["pipeline"]["log"]
        verdict = report.get("verdict", "?")
        log.append({"ts": store.now_iso().split("T")[1], "level":
                    "info" if verdict == "pass" else "warn",
                    "msg": f"实测完成（第{body.group + 1}组）：{verdict}——"
                           f"{report.get('summary', '')}"[:300]})
        del log[:-200]
    store.mutate(sid, _apply)
    return {"ok": True}


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

    application = FastAPI(title="游戏素材工坊", version="5.0.0")
    application.include_router(router, prefix="/api/v1")

    store.recover_interrupted()     # 启动时一次性清理上次进程留下的假 running
    store.ensure_item_ids()         # 条目稳定 id（方案编辑对账锚）
    mount_media(application)
    frontend = _Path(__file__).resolve().parents[1] / "frontend"
    mount_frontend(application, frontend, at="/", name="index")
    return application
