# -*- coding: utf-8 -*-
"""FastAPI 应用入口。

启动：
    cd backend
    python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
或直接使用项目根目录的 start.bat / start.sh。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app import blobs, repo
from app.api import comic as comic_api
from app.api import events, gallery, generate, portrait as portrait_api, system
from app.config import settings
from app.db import db
from app.inference import device as dev
from app.inference import scheduler
from app.inference.manager import manager
from app.services import generation


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- 启动 ----
    settings.ensure_dirs()
    # 漫画工作室的附属目录
    (settings.data_dir_path / "refs").mkdir(parents=True, exist_ok=True)
    (settings.output_dir_path / "comics").mkdir(parents=True, exist_ok=True)
    db.init_schema()

    # 历史出图按项目归位（早期版本所有图都混在 outputs/<日期>/ 里；幂等、只动有项目归属的图）
    try:
        from app.services import comic as comic_service
        comic_service.migrate_legacy_outputs()
        # 历史库存的绝对路径统一改写为相对路径（方便整目录迁移；幂等）
        comic_service.normalize_stored_paths()
    except Exception as exc:  # pragma: no cover - 防御性，绝不因整理失败而影响启动
        print(f"[comic] 历史出图归位跳过：{exc}")

    # 启动时清理「上次异常退出」遗留的中间态任务
    for job in repo.list_jobs(limit=200, status="running"):
        repo.mark_job_done(job["id"], "failed", error="服务重启导致中断")
    for job in repo.list_jobs(limit=200, status="queued"):
        repo.mark_job_done(job["id"], "failed", error="服务重启导致中断")

    # 任务参数里的参考图临时文件（data/blobs）此时全部作废：上面刚把排队中的
    # 任务标记为失败，它们的参数不会再被读取
    try:
        removed = blobs.gc_all()
        if removed:
            print(f"[启动] 清理任务参数临时文件 {removed} 份")
    except Exception as exc:  # pragma: no cover - 防御性
        print(f"[启动] 清理任务参数临时文件跳过：{exc}")

    consumer = asyncio.create_task(scheduler.consume_loop(generation.run_job))
    print(f"[启动] 运行模式 ENGINE_MODE={manager.mode}")
    print(f"[启动] 数据目录 {settings.data_dir_path}")
    print(f"[启动] 输出目录 {settings.output_dir_path}")
    print("[启动] 访问 http://127.0.0.1:%d" % settings.port)

    # local/comfy 模式下启动即在后台自动连接/加载，无需手动点「加载」
    if manager.mode in ("local", "comfy"):
        files = settings.model_files_status()
        if not files.get("ready"):
            print("[启动] 模型文件不完整，跳过自动加载（详见界面「模型下载」面板）")
        elif not dev.TORCH_AVAILABLE:
            print("[启动] 未安装 torch 推理依赖，跳过自动加载（pip install -r backend/requirements-inference.txt）")
        else:
            async def _auto_load() -> None:
                loop = asyncio.get_running_loop()
                print("[启动] 正在后台自动加载模型（首次约 1-3 分钟，期间可直接浏览界面）...")
                try:
                    await loop.run_in_executor(None, manager.ensure_loaded)
                    print(f"[启动] 模型自动加载完成，耗时 {manager.last_load_seconds}s")
                except Exception as exc:
                    print(f"[启动] 模型自动加载失败：{exc}")
                    print("       生成请求会再次尝试加载；若持续失败请查看界面模型状态中的报错信息。")

            asyncio.create_task(_auto_load())

    try:
        yield
    finally:
        consumer.cancel()
        try:
            await consumer
        except (asyncio.CancelledError, Exception):
            pass
        manager.unload()
        print("[退出] 已释放资源")


app = FastAPI(
    title="本地图片生成器 · Qwen-Image 2.1",
    version="1.0.0",
    description="基于 Qwen-Image 2.1 GGUF 的本地文生图 / 图生图服务",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- 路由 ----------------
app.include_router(generate.router, prefix="/api/v1")
app.include_router(events.router, prefix="/api/v1")
app.include_router(gallery.router, prefix="/api/v1")
app.include_router(system.router, prefix="/api/v1")
app.include_router(comic_api.router, prefix="/api/v1")
app.include_router(portrait_api.router, prefix="/api/v1")


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"code": "INTERNAL", "message": str(exc) or exc.__class__.__name__},
    )


# ---------------- 静态资源 ----------------
# 出图结果：/data/outputs/... 直接可访问
if settings.data_dir_path.exists():
    app.mount("/data", StaticFiles(directory=str(settings.data_dir_path)), name="data")

# 前端页面
_frontend = settings.frontend_dir_path
if _frontend.exists():

    @app.middleware("http")
    async def no_stale_js(request: Request, call_next):
        """JS/CSS 一律协商缓存（ETag 304 很便宜），杜绝改了代码浏览器还跑旧脚本。"""
        response = await call_next(request)
        path = request.url.path
        if path.endswith((".js", ".css")) and path.startswith("/static"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    def _static_version(name: str) -> str:
        try:
            return str(int((_frontend / name).stat().st_mtime))
        except OSError:
            return "0"

    @app.get("/", include_in_schema=False)
    async def index():
        # 给脚本/样式带上文件指纹（mtime），文件一变 URL 就变，强制浏览器拉新版
        html = (_frontend / "index.html").read_text(encoding="utf-8")
        for name in ("style.css", "app.js", "comic.js", "portrait.js"):
            html = html.replace(f"/{name}", f"/{name}?v={_static_version(name)}")
        return Response(content=html, media_type="text/html",
                        headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=str(_frontend)), name="static")
