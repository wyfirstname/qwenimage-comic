# -*- coding: utf-8 -*-
"""Web 装载小工具：门户与「独立运行的应用」共用同一套静态资源挂载方式。

这样应用无论被平台托管（挂在 /apps/<id>/）还是自己独立起进程（挂在自己
的根路径），前端资源都能用相对路径引用、媒体目录都指向同一份 data/。
"""

from __future__ import annotations

from pathlib import Path

from shared.config import settings


SHARED_WEB = Path(__file__).resolve().parent / "web"


def mount_media(application) -> None:
    """挂载前端共享运行时（/web）、出图结果（/data）与画风库缩略图（/style-thumbs）。

    /web 里是全平台共用的客户端资源（style.css / app.js），门户与每个应用的
    独立进程都指向同一份，避免三份拷贝各改一遍。
    """
    from fastapi.staticfiles import StaticFiles

    if SHARED_WEB.exists():
        application.mount("/web", StaticFiles(directory=str(SHARED_WEB)),
                          name="shared-web")
    if settings.data_dir_path.exists():
        application.mount("/data", StaticFiles(directory=str(settings.data_dir_path)),
                          name="data")
    try:
        from shared.style_library import THUMBS_DIR

        if THUMBS_DIR.exists():
            application.mount("/style-thumbs", StaticFiles(directory=str(THUMBS_DIR)),
                              name="style-thumbs")
    except Exception:  # pragma: no cover - 防御性
        pass


def mount_frontend(application, frontend: Path, at: str = "/", name: str = "frontend") -> bool:
    """把一个前端目录挂成静态站点（html=True 时自动提供 index.html）。

    应用前端内部的资源一律用**相对路径**（./app.js），因此挂在任何前缀下都成立。
    """
    from fastapi.staticfiles import StaticFiles

    if not frontend.exists():
        return False
    application.mount(at, StaticFiles(directory=str(frontend), html=True), name=name)
    return True
