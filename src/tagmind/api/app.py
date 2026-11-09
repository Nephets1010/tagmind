"""FastAPI 应用工厂。

**契约**（架构附录 A.4）：``create_app`` 必须**零参可调用**——Dockerfile 的
启动命令是 ``uvicorn tagmind.api.app:create_app --factory``，依赖从
``api/deps.py`` 内部解析。

阶段 0 只提供最小可用的骨架：应用工厂 + ``/healthz`` 健康探针 + 结构化日志
中间件 + 统一异常处理。业务路由（内容接入、标签查询、推荐）在阶段 1/3/4 实现。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tagmind import __version__
from tagmind.config.settings import Settings, get_settings
from tagmind.core.context import clear_context, new_request_id
from tagmind.core.errors import TagmindError
from tagmind.core.logging import configure_logging, get_logger

__all__ = ["create_app"]

logger = get_logger("tagmind.api")


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建 FastAPI 应用。

    Args:
        settings: 配置对象；``None`` 时取全局单例。

    Returns:
        配置好的 :class:`fastapi.FastAPI` 实例。
    """
    resolved = settings or get_settings()
    configure_logging(level=resolved.log_level, fmt=resolved.log_format)

    app = FastAPI(
        title="tagmind",
        version=__version__,
        description="企业级 LLM 驱动的智能打标与个性化内容分发系统",
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next: Any) -> Any:
        """注入 request_id 并记录访问日志。

        request_id 贯穿全链路：HTTP → contextvar → structlog → 响应头
        ``X-Request-ID`` → ``exposure_log.request_id``（架构 §14.4）。
        """
        request_id = request.headers.get("X-Request-ID") or new_request_id()
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            clear_context()
        duration_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "http.request",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round(duration_ms, 2),
        )
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(TagmindError)
    async def tagmind_error_handler(request: Request, exc: TagmindError) -> JSONResponse:
        """把领域异常渲染成统一错误体。

        响应体结构见架构 §14.3；``trace_id`` 必带，便于把用户看到的错误
        与服务端日志对上。
        """
        request_id = getattr(request.state, "request_id", None)
        payload = exc.to_payload(request_id)
        logger.warning(
            "http.error",
            request_id=request_id,
            code=exc.code,
            message=exc.message,
        )
        return JSONResponse(status_code=exc.http_status, content=payload)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, Any]:
        """容器健康探针。

        Dockerfile 的 ``HEALTHCHECK`` 与 ``docker-compose`` 的 healthcheck 都
        指向这个路径——既有契约，路径不改为 ``/health``（架构附录 A.4）。
        """
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["ops"])
    async def readyz() -> dict[str, Any]:
        """就绪探针：检查数据库连通性。"""
        from tagmind.db.session import ping

        database_ok = ping()
        return {
            "status": "ok" if database_ok else "degraded",
            "database": "up" if database_ok else "down",
        }

    return app
