# -*- coding: utf-8 -*-
"""FastAPI 应用工厂：装配业务库/任务管理器/路由/静态托管。

启动流程（lifespan）：
1. 业务库幂等建表（migrations.sql）；
2. 遗留 running/queued 任务标记为 failed（服务重启中断）；
3. 启动任务 worker 线程。

前端构建产物（frontend/ 的 Vite build 输出到本包 static/）以
StaticFiles(html=True) 挂在 "/" 末尾兜底，/api/* 路由优先匹配；
hash 路由无需后端 rewrite。static/ 不存在时根路径提示先构建前端。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import HTTPException

from .. import __version__
from . import api
from .cleaner import UploadCleaner
from .config import WebConfig
from .db import ensure_meta_schema
from ..tasks import TaskManager, TaskStore


def _build_app(config: WebConfig) -> FastAPI:
    store = TaskStore(config.management, schema=config.management.schema)
    manager = TaskManager(store, workers=config.workers)
    cleaner = UploadCleaner(store, config.uploads_abs(), config.upload_ttl_days)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ensure_meta_schema(config.management, config.management.schema)
        store.orphan_recover("服务重启中断（未完成任务标记为失败）")
        manager.start()
        cleaner.start()
        yield
        cleaner.stop()
        manager.shutdown()

    app = FastAPI(
        title="gdb2pg Web 任务管理",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.web_config = config
    app.state.store = store
    app.state.manager = manager

    # 统一错误响应：{ok: false, error: ...}
    @app.exception_handler(HTTPException)
    async def _http_exc_handler(request: Request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"ok": False, "error": exc.detail},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):  # pragma: no cover
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": f"服务器内部错误: {exc}"},
        )

    app.include_router(api.router)

    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        # 挂在末尾：仅兜底非 /api 请求；html=True 支持直接访问 index.html
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
    else:

        @app.get("/")
        def _no_frontend():
            return HTMLResponse(
                "<h3>gdb2pg Web 前端尚未构建</h3>"
                "<p>请执行 <code>cd frontend && npm install && npm run build</code>，"
                "或用 <code>npm run dev</code> 开发模式（访问 http://localhost:5173）。</p>"
                "<p>后端 API 正常：<a href=\"/api/task-types\">/api/task-types</a></p>")

    return app


def create_app(config: Optional[WebConfig] = None, config_path: Optional[str] = None) -> FastAPI:
    """应用工厂。可直接给 uvicorn 的工厂模式或 python -m gdb2pg web 使用。"""
    cfg = config or WebConfig.load(config_path)
    return _build_app(cfg)