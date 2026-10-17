# -*- coding: utf-8 -*-
"""应用注册表：扫描 apps/*/manifest.json，把各应用挂到平台上。

门户只做「发现与挂载」，不掺和任何业务逻辑。新增一个功能 = 在 apps/ 下加一个
目录（含 manifest.json 与 backend/routes.py），门户会自动把它挂上、自动出现在
卡片墙。平台代码一行都不用改。
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

from shared.config import settings
from shared.webapp import mount_frontend


def apps_dir() -> Path:
    return settings.apps_dir_path


def load_manifests() -> list[dict]:
    """读取所有应用的 manifest（按 id 排序，稳定可预期）。"""
    out: list[dict] = []
    root = apps_dir()
    if not root.exists():
        return out
    for d in sorted(root.iterdir()):
        manifest_file = d / "manifest.json"
        if not (d.is_dir() and manifest_file.exists()):
            continue
        try:
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - 防御性
            print(f"[portal] 读取 {manifest_file} 失败：{exc}")
            continue
        data.setdefault("id", d.name)
        data["_dir"] = str(d)
        out.append(data)
    return out


def backend_package(app_id: str):
    """导入应用后端包（会触发其 schema / 回调的注册副作用）。"""
    return importlib.import_module(f"apps.{app_id}.backend")


def routes_module(app_id: str):
    return importlib.import_module(f"apps.{app_id}.backend.routes")


def mount_all(application) -> list[dict]:
    """把每个应用的路由挂到平台，并把各自前端静态托管在 /apps/<id>/。"""
    manifests = load_manifests()
    for m in manifests:
        app_id = m["id"]
        try:
            routes = routes_module(app_id)
            application.include_router(routes.router, prefix="/api/v1")
        except Exception as exc:  # pragma: no cover - 防御性
            print(f"[portal] 挂载应用 {app_id} 失败：{exc}")
            continue
        frontend = Path(m["_dir"]) / "frontend"
        mount_frontend(application, frontend, at=f"/apps/{app_id}",
                       name=f"app-{app_id}")
    return manifests


def run_startups() -> None:
    """执行各应用声明的启动钩子（可选）：应用包里的 startup()。"""
    for m in load_manifests():
        try:
            pkg = backend_package(m["id"])
        except Exception as exc:  # pragma: no cover - 防御性
            print(f"[portal] 载入应用 {m['id']} 失败：{exc}")
            continue
        hook = getattr(pkg, "startup", None)
        if callable(hook):
            try:
                hook()
            except Exception as exc:  # pragma: no cover - 防御性
                print(f"[portal] 应用 {m['id']} 启动钩子失败：{exc}")


def startup_hooks() -> list:
    """返回所有应用的 startup 钩子（供门户在合适的时机统一调用）。"""
    hooks = []
    for m in load_manifests():
        try:
            pkg = backend_package(m["id"])
        except Exception:
            continue
        hook = getattr(pkg, "startup", None)
        if callable(hook):
            hooks.append(hook)
    return hooks
