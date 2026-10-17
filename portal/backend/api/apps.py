# -*- coding: utf-8 -*-
"""平台级：应用清单（门户卡片墙的数据源）。"""

from __future__ import annotations

from fastapi import APIRouter

from portal.backend import registry

router = APIRouter(tags=["apps"])


def _public(manifest: dict) -> dict:
    """对外暴露的应用信息（过滤内部字段，补上入口地址）。"""
    app_id = manifest["id"]
    return {
        "id": app_id,
        "name": manifest.get("name", app_id),
        "icon": manifest.get("icon", "▦"),
        "description": manifest.get("description", ""),
        "version": manifest.get("version", ""),
        "capabilities": manifest.get("capabilities", []),
        "api_prefix": manifest.get("api_prefix", f"/api/v1/{app_id}"),
        "standalone_port": manifest.get("standalone_port"),
        "entry": f"/apps/{app_id}/",
    }


@router.get("/apps", summary="应用清单")
async def list_apps():
    return {"items": [_public(m) for m in registry.load_manifests()]}
