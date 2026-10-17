# -*- coding: utf-8 -*-
"""平台门户入口（上层）。

门户只做两件事：把引擎启起来、把各应用挂上去。所有业务逻辑都在 apps/ 里。

启动：
    python -m uvicorn portal.backend.main:app --host 127.0.0.1 --port 8000
或项目根目录的 run_platform.bat。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from engine.sdk import engine
from portal.backend import registry
from portal.backend.api import apps as apps_api
from portal.backend.api import events, gallery, system
from shared.config import settings
from shared.webapp import mount_media


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1) 引擎（最底一层）：启动它自己（建表 / 清残留 / 消费循环 / 自动加载）
    await engine.startup()
    # 2) 各应用的启动整理（漫画的历史出图归位、状态收敛等）
    registry.run_startups()

    print("[portal] 已挂载应用：" +
          ", ".join(m["id"] for m in registry.load_manifests()))
    print("[portal] 访问 http://127.0.0.1:%d" % settings.port)
    try:
        yield
    finally:
        await engine.shutdown()


app = FastAPI(
    title="Qwen-Image 工作台",
    version="1.0.0",
    description="本地 Qwen-Image 平台：图片生成 / 漫画工作室 / 人像编辑",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- 平台级路由 ----------------
app.include_router(system.router, prefix="/api/v1")
app.include_router(gallery.router, prefix="/api/v1")
app.include_router(events.router, prefix="/api/v1")
app.include_router(apps_api.router, prefix="/api/v1")

# ---------------- 挂载各应用（路由 + 前端）----------------
MANIFESTS = registry.mount_all(app)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"code": "INTERNAL", "message": str(exc) or exc.__class__.__name__},
    )


# ---------------- 共享静态资源 ----------------
mount_media(app)  # /data、/style-thumbs

# ---------------- 门户自身前端 ----------------
_frontend = settings.portal_frontend_path
if _frontend.exists():

    @app.middleware("http")
    async def no_stale_js(request: Request, call_next):
        """JS/CSS 一律协商缓存（ETag 304 很便宜），杜绝改了代码还跑旧脚本。"""
        response = await call_next(request)
        path = request.url.path
        if path.endswith((".js", ".css")):
            response.headers["Cache-Control"] = "no-cache"
        return response

    def _static_version(name: str) -> str:
        try:
            return str(int((_frontend / name).stat().st_mtime))
        except OSError:
            return "0"

    @app.get("/", include_in_schema=False)
    async def index():
        html = (_frontend / "index.html").read_text(encoding="utf-8")
        for name in ("portal.css", "portal.js"):
            html = html.replace(f"/static/{name}", f"/static/{name}?v={_static_version(name)}")
        return Response(content=html, media_type="text/html",
                        headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=str(_frontend)), name="portal-static")
