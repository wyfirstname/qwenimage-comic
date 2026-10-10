# -*- coding: utf-8 -*-
"""漫画工作室接口：项目 / 角色 / 分镜 / 出图 / 排版导出。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app import comic_repo as crepo
from app.schemas import (
    ComicCharacterIn,
    ComicCharacterPatch,
    ComicExportIn,
    ComicExtractIn,
    ComicGenerateIn,
    ComicMoveIn,
    ComicPanelIn,
    ComicPanelPatch,
    ComicProjectIn,
    ComicProjectPatch,
    ComicRefIn,
    ComicRenderIn,
    ComicSceneIn,
    ComicScenePatch,
    ComicScriptContinueIn,
    ComicScriptGenIn,
    ComicScriptIn,
    ComicScriptPolishIn,
    OkOut,
)
from app.services import comic as cs
from app.services import comic_ai as cai
from app.services.comic import ComicError

router = APIRouter(tags=["comic"], prefix="/comic")


def _fail(exc: Exception, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": "COMIC_ERROR", "message": str(exc)})


def _require_project(project_id: str) -> dict:
    project = crepo.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "项目不存在"})
    return project


def _require_panel(panel_id: str) -> dict:
    panel = crepo.get_panel(panel_id)
    if not panel:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "分镜不存在"})
    return panel


def _require_scene(scene_id: str) -> dict:
    scene = crepo.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "场景不存在"})
    return scene


# ---------------- 预设 ----------------
@router.get("/presets", summary="风格 / 主题色 / 景别 / 排版 / 档位 / 节奏模板预设")
async def presets():
    return cs.presets()


@router.get("/styles", summary="画风库列表（分组 / 搜索 / 分页）")
async def list_styles(group: str = "", keyword: str = "", page: int = 1, page_size: int = 60):
    """全量 327 条太大，改由前端分页拉取，别一次全吐给浏览器。

    group 为空时返回全部（含「常用」5 个旧画风）。
    keyword 命中 key / 名称 / 参考作者。
    """
    from app.style_library import library as sl

    items = sl().list_styles()
    if group:
        items = [x for x in items if (x.get("group") or "") == group]
    kw = (keyword or "").strip().lower()
    if kw:
        items = [x for x in items if kw in (
            f"{x.get('key','')} {x.get('name','')} {x.get('name_en','')} "
            f"{x.get('reference','')} {x.get('group','')}".lower())]
    total = len(items)
    page = max(1, int(page or 1))
    page_size = max(1, min(200, int(page_size or 60)))
    start = (page - 1) * page_size
    chunk = items[start:start + page_size]
    out = []
    for s in chunk:
        out.append({
            "key": s.get("key"),
            "group": s.get("group") or "",
            "name": s.get("name") or "",
            "name_en": s.get("name_en") or "",
            "reference": s.get("reference") or "",
            "legacy": bool(s.get("legacy")),
            "thumb": sl().thumb_url(s.get("key")),
        })
    return {"total": total, "page": page, "page_size": page_size, "items": out,
            "groups": sl().groups()}


@router.get("/styles/{key}", summary="单个画风详情（含 traits 长文本与缩略图）")
async def style_detail(key: str):
    try:
        return cs.style_detail(key)
    except Exception as exc:  # pragma: no cover - 防御性
        raise _fail(exc, 404)


@router.get("/rhythms", summary="分镜节奏模板列表（handraw SB-*）")
async def list_rhythms():
    from app import rhythm
    from app.style_library import library as sl

    return {
        "items": [
            {**x, "detail": rhythm.build(x.get("id"))}
            for x in sl().sb_templates()
        ],
        "families": rhythm.list_families(),
    }


@router.get("/defaults", summary="默认提示词（反向提示词等）")
async def defaults():
    """前端「恢复默认反提示词」用，保证和后端同一份文本。"""
    from app.comic_defaults import (
        DEFAULT_CHAR_NEGATIVE,
        DEFAULT_PROJECT_NEGATIVE,
    )

    return {
        "project_negative": DEFAULT_PROJECT_NEGATIVE,
        "char_negative": DEFAULT_CHAR_NEGATIVE,
        "ref_strength": 0.55,
    }


# ---------------- 项目 ----------------
@router.get("/projects", summary="项目列表")
async def list_projects():
    return {"items": crepo.list_projects()}


@router.post("/projects", summary="新建漫画项目")
async def create_project(req: ComicProjectIn):
    return cs.new_project_from_payload(req.model_dump())


@router.get("/projects/{project_id}", summary="项目详情（含角色/分镜/成品）")
async def project_detail(project_id: str):
    try:
        return cs.project_detail(project_id)
    except ComicError as exc:
        raise _fail(exc, 404)


@router.patch("/projects/{project_id}", summary="更新项目设置")
async def patch_project(project_id: str, req: ComicProjectPatch):
    _require_project(project_id)
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    crepo.update_project(project_id, **fields)
    return crepo.get_project(project_id)


@router.delete("/projects/{project_id}", summary="删除项目", response_model=OkOut)
async def delete_project(project_id: str):
    _require_project(project_id)
    crepo.delete_project(project_id)
    return OkOut(message="项目已删除")


# ---------------- 剧本 → 分镜 ----------------
@router.post("/projects/{project_id}/script/generate", summary="AI 按梗概写剧本（后台任务）")
async def ai_generate_script(project_id: str, req: ComicScriptGenIn):
    _require_project(project_id)
    try:
        return cai.ai_tasks.submit("script_generate", project_id,
                                   idea=req.idea, pages=req.pages)
    except cai.LocalTextError as exc:
        raise _fail(exc, 409)


@router.post("/projects/{project_id}/script/continue", summary="AI 续写剧本（后台任务）")
async def ai_continue_script(project_id: str, req: ComicScriptContinueIn):
    _require_project(project_id)
    try:
        return cai.ai_tasks.submit("script_continue", project_id,
                                   idea=req.idea, pages=req.pages)
    except cai.LocalTextError as exc:
        raise _fail(exc, 409)


@router.post("/projects/{project_id}/script/polish", summary="AI 润色剧本（后台任务）")
async def ai_polish_script(project_id: str, req: ComicScriptPolishIn):
    _require_project(project_id)
    try:
        return cai.ai_tasks.submit("script_polish", project_id, script=req.text)
    except cai.LocalTextError as exc:
        raise _fail(exc, 409)


@router.post("/projects/{project_id}/characters/extract", summary="AI 从剧本提取角色（后台任务）")
async def ai_extract_characters(project_id: str, req: ComicExtractIn):
    _require_project(project_id)
    try:
        return cai.ai_tasks.submit("characters_extract", project_id,
                                   script=req.script, count=req.count, replace=req.replace)
    except cai.LocalTextError as exc:
        raise _fail(exc, 409)


@router.post("/projects/{project_id}/scenes/extract", summary="AI 从剧本提取场景（后台任务）")
async def ai_extract_scenes(project_id: str, req: ComicExtractIn):
    _require_project(project_id)
    try:
        return cai.ai_tasks.submit("scenes_extract", project_id,
                                   script=req.script, count=req.count, replace=req.replace)
    except cai.LocalTextError as exc:
        raise _fail(exc, 409)


@router.post("/projects/{project_id}/storyboard", summary="AI 智能分镜（后台任务）")
async def ai_storyboard(project_id: str, req: ComicScriptIn):
    _require_project(project_id)
    try:
        return cai.ai_tasks.submit("storyboard", project_id,
                                   script=req.text, replace=req.replace)
    except cai.LocalTextError as exc:
        raise _fail(exc, 409)


@router.post("/projects/{project_id}/script", summary="解析剧本生成分镜（规则拆格）")
async def parse_script(project_id: str, req: ComicScriptIn):
    _require_project(project_id)
    try:
        return cs.create_panels_from_script(project_id, req.text, replace=req.replace)
    except ComicError as exc:
        raise _fail(exc)


@router.post("/projects/{project_id}/script/preview", summary="剧本解析预览（不落库）")
async def preview_script(project_id: str, req: ComicScriptIn):
    _require_project(project_id)
    items = cs.parse_script(req.text)
    return {"count": len(items), "items": items}


# ---------------- 角色 ----------------
@router.post("/projects/{project_id}/characters", summary="新增角色")
async def create_character(project_id: str, req: ComicCharacterIn):
    _require_project(project_id)
    return crepo.create_character(project_id, **req.model_dump())


@router.patch("/characters/{character_id}", summary="更新角色")
async def patch_character(character_id: str, req: ComicCharacterPatch):
    if not crepo.get_character(character_id):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "角色不存在"})
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    crepo.update_character(character_id, **fields)
    return crepo.get_character(character_id)


@router.delete("/characters/{character_id}", summary="删除角色", response_model=OkOut)
async def delete_character(character_id: str):
    if not crepo.get_character(character_id):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "角色不存在"})
    crepo.delete_character(character_id)
    return OkOut(message="角色已删除")


@router.post("/characters/{character_id}/ref", summary="上传角色参考图（可选，覆盖本地生成的形象图）")
async def upload_character_ref(character_id: str, req: ComicRefIn):
    if not crepo.get_character(character_id):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "角色不存在"})
    try:
        return cs.save_character_ref(character_id, req.image)
    except ComicError as exc:
        raise _fail(exc)


def _require_character(character_id: str) -> dict:
    char = crepo.get_character(character_id)
    if not char:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "角色不存在"})
    return char


@router.get("/characters/{character_id}/prompt", summary="查看角色形象图实际提示词")
async def character_prompt(character_id: str):
    char = _require_character(character_id)
    project = crepo.get_project(char["project_id"]) or {}
    prompt, negative = cs.build_character_prompt(project, char)
    return {"prompt": prompt, "negative_prompt": negative, "name": char["name"]}


@router.get("/characters/{character_id}/status", summary="角色形象图出图状态")
async def character_status(character_id: str):
    char = _require_character(character_id)
    return {
        "id": char["id"],
        "status": char.get("status") or "draft",
        "job_id": char.get("job_id"),
        "image_id": char.get("image_id"),
        "image_url": char.get("image_url"),
        "ref_url": char.get("ref_url"),
        "ref_source": char.get("ref_source") or "",
        "seed": char.get("seed"),
        "error": char.get("error"),
    }


@router.post("/characters/{character_id}/generate", summary="本地生成角色形象图")
async def generate_character_image(character_id: str):
    _require_character(character_id)
    try:
        return await cs.enqueue_character_image(character_id)
    except ComicError as exc:
        raise _fail(exc)


@router.post("/projects/{project_id}/characters/generate", summary="批量生成角色形象图")
async def generate_all_characters(project_id: str, only_missing: bool = True):
    _require_project(project_id)
    try:
        return await cs.enqueue_characters(project_id, only_missing=only_missing)
    except ComicError as exc:
        raise _fail(exc)


# ---------------- 本地 AI 任务（剧本 / 角色提取 / 场景提取 / 智能分镜 ----------------
# 本机文本推理慢（分钟级），统一走后台任务 + 轮询，避免 HTTP 超时。


@router.get("/llm/status", summary="本地文本模型状态")
async def llm_status():
    return cai.llm_status()


@router.post("/ai/tasks", summary="提交本地 AI 任务")
async def create_ai_task(payload: dict):
    project_id = str(payload.get("project_id") or "")
    kind = str(payload.get("kind") or "")
    _require_project(project_id)
    params = {k: v for k, v in payload.items() if k not in ("project_id", "kind")}
    try:
        return cai.ai_tasks.submit(kind, project_id, **params)
    except cai.LocalTextError as exc:
        raise _fail(exc, 409)


@router.get("/ai/tasks", summary="AI 任务列表")
async def list_ai_tasks(project_id: str = "", limit: int = 10):
    return {"items": cai.ai_tasks.list_recent(project_id or None, limit=limit),
            "running": cai.ai_tasks.running()}


@router.get("/ai/tasks/{task_id}", summary="AI 任务状态")
async def get_ai_task(task_id: str):
    task = cai.ai_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "任务不存在"})
    return task


@router.post("/ai/tasks/{task_id}/cancel", summary="取消 AI 任务", response_model=OkOut)
async def cancel_ai_task(task_id: str):
    if not cai.ai_tasks.cancel(task_id):
        raise _fail(ComicError("任务不存在或已结束"), 409)
    return OkOut(message="已取消")


# ---------------- 场景 ----------------
@router.post("/projects/{project_id}/scenes", summary="新增场景")
async def create_scene(project_id: str, req: ComicSceneIn):
    _require_project(project_id)
    return crepo.create_scene(project_id, **req.model_dump())


@router.patch("/scenes/{scene_id}", summary="更新场景")
async def patch_scene(scene_id: str, req: ComicScenePatch):
    _require_scene(scene_id)
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    crepo.update_scene(scene_id, **fields)
    return crepo.get_scene(scene_id)


@router.get("/scenes/{scene_id}", summary="场景详情")
async def get_scene(scene_id: str):
    return _require_scene(scene_id)


@router.get("/scenes/{scene_id}/status", summary="场景概念图出图状态")
async def get_scene_status(scene_id: str):
    scene = _require_scene(scene_id)
    return {
        "id": scene["id"],
        "status": scene.get("status") or "draft",
        "job_id": scene.get("job_id"),
        "image_id": scene.get("image_id"),
        "image_url": scene.get("image_url"),
        "error": scene.get("error"),
    }


@router.delete("/scenes/{scene_id}", summary="删除场景", response_model=OkOut)
async def delete_scene(scene_id: str):
    _require_scene(scene_id)
    crepo.delete_scene(scene_id)
    return OkOut(message="场景已删除")


@router.post("/scenes/{scene_id}/move", summary="调整场景顺序", response_model=OkOut)
async def move_scene(scene_id: str, req: ComicMoveIn):
    _require_scene(scene_id)
    crepo.swap_scene_seq(scene_id, 1 if req.direction >= 0 else -1)
    return OkOut(message="已调整")


@router.get("/scenes/{scene_id}/prompt", summary="查看场景概念图实际提示词")
async def scene_prompt(scene_id: str):
    scene = _require_scene(scene_id)
    project = crepo.get_project(scene["project_id"]) or {}
    prompt, negative = cs.build_scene_prompt(project, scene)
    return {"prompt": prompt, "negative_prompt": negative, "name": scene["name"]}


@router.post("/scenes/{scene_id}/generate", summary="生成场景概念图")
async def generate_scene(scene_id: str):
    _require_scene(scene_id)
    try:
        return await cs.enqueue_scene_image(scene_id)
    except ComicError as exc:
        raise _fail(exc, 409)


@router.post("/projects/{project_id}/scenes/generate", summary="批量生成场景概念图")
async def generate_scenes(project_id: str, req: ComicGenerateIn):
    _require_project(project_id)
    try:
        return await cs.enqueue_project_scenes(project_id, only_missing=req.only_missing)
    except ComicError as exc:
        raise _fail(exc, 409)


# ---------------- 分镜 ----------------
@router.post("/projects/{project_id}/panels", summary="新增分镜")
async def create_panel(project_id: str, req: ComicPanelIn):
    _require_project(project_id)
    seq = crepo.next_seq(project_id)
    panel = crepo.create_panel(project_id, seq=seq, status="draft", **req.model_dump())
    return cs.panel_to_dict(panel)


@router.patch("/panels/{panel_id}", summary="更新分镜")
async def patch_panel(panel_id: str, req: ComicPanelPatch):
    _require_panel(panel_id)
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    crepo.update_panel(panel_id, **fields)
    return cs.panel_to_dict(crepo.get_panel(panel_id))


@router.delete("/panels/{panel_id}", summary="删除分镜", response_model=OkOut)
async def delete_panel(panel_id: str):
    panel = _require_panel(panel_id)
    crepo.delete_panel(panel_id)
    project = crepo.get_project(panel["project_id"]) or {}
    crepo.renumber(panel["project_id"], page_size=cs.page_size_for_layout(project.get("layout")))
    return OkOut(message="分镜已删除")


@router.post("/panels/{panel_id}/move", summary="调整分镜顺序")
async def move_panel(panel_id: str, req: ComicMoveIn):
    panel = _require_panel(panel_id)
    crepo.swap_seq(panel_id, -1 if req.direction < 0 else 1)
    project = crepo.get_project(panel["project_id"]) or {}
    crepo.renumber(panel["project_id"], page_size=cs.page_size_for_layout(project.get("layout")))
    return {"items": [cs.panel_to_dict(p) for p in crepo.list_panels(panel["project_id"])]}


@router.get("/panels/{panel_id}/prompt", summary="查看分镜实际提示词与一致性参数")
async def panel_prompt(panel_id: str):
    _require_panel(panel_id)
    try:
        return cs.panel_generation_info(panel_id)
    except ComicError as exc:
        raise _fail(exc, 404)


@router.post("/panels/{panel_id}/generate", summary="生成 / 重绘单格")
async def generate_panel(panel_id: str, strength: float | None = None,
                         reseed: bool = True):
    """reseed=True（默认）每次换随机种子，重绘必然出不同的图；
    勾「锁定种子」时传 reseed=false 复用上次种子做复现。"""
    _require_panel(panel_id)
    try:
        return await cs.enqueue_panel(panel_id, strength=strength, reseed=reseed)
    except ComicError as exc:
        raise _fail(exc, 409)


@router.post("/projects/{project_id}/generate", summary="批量生成整部漫画分镜")
async def generate_project(project_id: str, req: ComicGenerateIn):
    _require_project(project_id)
    try:
        return await cs.enqueue_project(project_id, only_missing=req.only_missing,
                                        strength=req.strength, reseed=req.reseed)
    except ComicError as exc:
        raise _fail(exc, 409)


# ---------------- 排版导出 ----------------
@router.post("/projects/{project_id}/render", summary="排版渲染成品页")
async def render_project(project_id: str, req: ComicRenderIn):
    _require_project(project_id)
    try:
        return cs.render_project_pages(
            project_id,
            layout=req.layout,
            page_width=req.page_width,
            bubble_position=req.bubble_position,
            grayscale=req.grayscale,
            show_page_number=req.show_page_number,
        )
    except ComicError as exc:
        raise _fail(exc)


@router.get("/projects/{project_id}/renders", summary="成品页列表")
async def list_renders(project_id: str):
    _require_project(project_id)
    return {"items": crepo.list_renders(project_id, limit=30)}


@router.delete("/renders/{render_id}", summary="删除成品页", response_model=OkOut)
async def delete_render(render_id: str):
    crepo.delete_render(render_id)
    return OkOut(message="已删除")


@router.post("/projects/{project_id}/export", summary="导出整部漫画为 PDF")
async def export_project_pdf(project_id: str, req: ComicExportIn):
    _require_project(project_id)
    try:
        return cs.export_project_pdf(
            project_id,
            layout=req.layout,
            page_width=req.page_width,
            bubble_position=req.bubble_position,
            show_page_number=req.show_page_number,
            rerender=req.rerender,
            color_mode=req.color_mode,
        )
    except ComicError as exc:
        raise _fail(exc)


@router.delete("/projects/{project_id}/exports/{filename}", summary="删除导出的 PDF", response_model=OkOut)
async def delete_project_export(project_id: str, filename: str):
    _require_project(project_id)
    try:
        cs.delete_project_export(project_id, filename)
    except ComicError as exc:
        raise _fail(exc, 404)
    return OkOut(message="已删除")
