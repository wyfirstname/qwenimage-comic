# -*- coding: utf-8 -*-
"""图库接口。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from app import repo
from app.config import settings
from app.models_util import resolve_stored
from app.schemas import GalleryPage, OkOut

router = APIRouter(tags=["gallery"])


@router.get("/gallery", response_model=GalleryPage, summary="图库列表（分页）")
async def list_gallery(
    page: int = 1,
    page_size: int = 24,
    keyword: str = "",
    favorite_only: bool = False,
    job_id: str | None = None,
):
    total, items = repo.query_gallery(
        page=max(1, page),
        page_size=max(1, min(page_size, 100)),
        keyword=keyword.strip(),
        favorite_only=favorite_only,
        job_id=job_id,
    )
    return {"total": total, "page": page, "page_size": page_size, "items": items}


@router.get("/gallery/stats", summary="图库统计")
async def stats():
    return repo.gallery_stats()


@router.post("/gallery/{image_id}/favorite", response_model=OkOut, summary="收藏 / 取消收藏")
async def toggle_favorite(image_id: str, favorite: bool = True):
    img = repo.get_image(image_id)
    if not img:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "图片不存在"})
    repo.set_favorite(image_id, favorite)
    return {"ok": True, "message": "已更新"}


@router.delete("/gallery/{image_id}", response_model=OkOut, summary="删除图片")
async def delete_image(image_id: str, remove_file: bool = True):
    img = repo.delete_image(image_id)
    if not img:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "图片不存在"})
    if remove_file:
        try:
            # path 存的是相对路径（data/...），也能兼容历史绝对路径
            p = resolve_stored(img["path"]) or (settings.project_root / str(img["path"]))
            p.unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True, "message": "已删除"}
