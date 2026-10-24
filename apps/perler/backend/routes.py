# -*- coding: utf-8 -*-
"""拼豆工作台接口。

无引擎依赖：整个 app 是纯 Pillow 图像处理，不吃显存、不进 slots，
跟引擎忙不忙完全无关，任何一台 16G/8G 机器都能跑。
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from PIL import Image

from shared.config import settings
from shared.models_util import new_id

from . import beads
from .palettes import get_palette, load_palettes

router = APIRouter(prefix="/perler", tags=["perler"])

# 允许的图片格式
_ALLOWED_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"}
_MAX_UPLOAD = 20 * 1024 * 1024  # 20MB，手机原图也够

_PAT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _pattern_dir(pattern_id: str) -> Path:
    if not _PAT_ID.fullmatch(pattern_id or ""):
        raise HTTPException(status_code=400, detail={"code": "BAD_ID", "message": "非法图纸 ID"})
    return settings.data_dir_path / "perler" / "patterns" / pattern_id


@router.get("/palettes", summary="色板列表")
async def list_palettes():
    return {
        "items": [
            {"id": p["id"], "name": p["name"], "count": len(p["colors"])}
            for p in load_palettes().values()
        ]
    }


@router.post("/convert", summary="图片转拼豆图纸")
async def convert(
    image: UploadFile = File(..., description="源图片"),
    width: int = Form(58, description="豆板宽度（豆数），高度按比例自动算"),
    palette: str = Form("common48", description="色板 ID"),
    dither: bool = Form(False, description="Floyd-Steinberg 抖动（渐变图开，图标关）"),
):
    if image.content_type not in _ALLOWED_TYPES:
        raise HTTPException(
            status_code=415,
            detail={"code": "BAD_TYPE", "message": f"不支持的图片类型：{image.content_type}"},
        )
    data = await image.read()
    if len(data) > _MAX_UPLOAD:
        raise HTTPException(status_code=413, detail={"code": "TOO_LARGE", "message": "图片超过 20MB"})
    try:
        result = beads.convert_image(data, width=width, palette_id=palette, dither=dither)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail={"code": "BAD_PALETTE", "message": str(exc)})
    except Exception as exc:
        raise HTTPException(status_code=400, detail={"code": "BAD_IMAGE", "message": f"图片解析失败：{exc}"})

    pattern_id = new_id("pat")
    pdir = settings.data_dir_path / "perler" / "patterns" / pattern_id
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "source.png").write_bytes(_to_png(data))
    (pdir / "preview.png").write_bytes(result.pop("preview"))
    palette_meta = get_palette(palette)
    (pdir / "pattern.json").write_text(
        json.dumps({
            "id": pattern_id,
            "width": result["width"],
            "height": result["height"],
            "palette": palette,
            "palette_name": result["palette_name"],
            "dither": dither,
            "grid": result["grid"],
            "colors": result["colors"],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    return {
        "id": pattern_id,
        "width": result["width"],
        "height": result["height"],
        "total": result["total"],
        "palette": palette,
        "palette_name": result["palette_name"],
        "dither": dither,
        "colors": result["colors"],
        "preview_url": f"/api/v1/perler/patterns/{pattern_id}/preview.png",
        "sheet_url": f"/api/v1/perler/patterns/{pattern_id}/sheet.png",
        "json_url": f"/api/v1/perler/patterns/{pattern_id}/pattern.json",
    }


@router.get("/patterns/{pattern_id}/preview.png", summary="逐豆像素预览图")
async def pattern_preview(pattern_id: str):
    pdir = _pattern_dir(pattern_id)
    f = pdir / "preview.png"
    if not f.exists():
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "图纸不存在"})
    return Response(content=f.read_bytes(), media_type="image/png")


@router.get("/patterns/{pattern_id}/sheet.png", summary="可打印图纸（网格+坐标+图例）")
async def pattern_sheet(pattern_id: str):
    pdir = _pattern_dir(pattern_id)
    meta = pdir / "pattern.json"
    if not meta.exists():
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "图纸不存在"})
    data = json.loads(meta.read_text(encoding="utf-8"))
    png = beads.render_sheet_png(
        grid=data["grid"],
        colors=data["colors"],
        palette_colors=get_palette(data["palette"])["colors"],
    )
    return Response(
        content=png,
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{pattern_id}_sheet.png"'},
    )


@router.get("/patterns/{pattern_id}/pattern.json", summary="图纸数据（网格+清单）")
async def pattern_json(pattern_id: str):
    pdir = _pattern_dir(pattern_id)
    meta = pdir / "pattern.json"
    if not meta.exists():
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "图纸不存在"})
    return JSONResponse(content=json.loads(meta.read_text(encoding="utf-8")))


def _to_png(data: bytes) -> bytes:
    """统一把源图落成 PNG，避免后续格式纠结。"""
    try:
        img = Image.open(io.BytesIO(data))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return data


# ============================================================
#  独立运行入口（不进平台，单独起一个进程）
# ============================================================
def create_app():
    """uvicorn apps.perler.backend.routes:create_app --factory --port 8106"""
    from pathlib import Path as _Path

    from fastapi import FastAPI

    from shared.webapp import mount_frontend, mount_media

    application = FastAPI(title="拼豆工作台", version="1.0.0")
    application.include_router(router, prefix="/api/v1")

    mount_media(application)  # /web 共享样式与脚本（独立模式必须挂，门户模式由门户统一挂）
    frontend = _Path(__file__).resolve().parents[1] / "frontend"
    mount_frontend(application, frontend, at="/", name="index")
    return application
