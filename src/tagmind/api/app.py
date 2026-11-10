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
from starlette.concurrency import run_in_threadpool

from tagmind import __version__
from tagmind.config.settings import Settings, get_settings
from tagmind.core.context import context_scope, new_request_id
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
        """注入 request_id、绑定上下文、记录访问日志。

        request_id 贯穿全链路：HTTP → contextvar → structlog → 响应头
        ``X-Request-ID`` → ``exposure_log.request_id``（架构 §14.4）。

        用 ``context_scope`` 而非 ``bind_context``：后者在多字段场景只
        reset 一个变量，会把 request_id 泄漏到下一条复用线程的请求。
        """
        request_id = request.headers.get("X-Request-ID") or new_request_id()
        request.state.request_id = request_id
        started = time.perf_counter()

        with context_scope(request_id=request_id, trace_id=request_id):
            try:
                response = await call_next(request)
            except Exception:
                # 异常路径此前既没有访问日志、也没有 X-Request-ID 响应头，
                # 线上 500 无法与日志对上。这里补齐两者并保留统一错误体。
                duration_ms = (time.perf_counter() - started) * 1000
                logger.error(
                    "http.request",
                    request_id=request_id,
                    method=request.method,
                    path=request.url.path,
                    status_code=500,
                    duration_ms=round(duration_ms, 2),
                    exc_info=True,
                )
                response = JSONResponse(
                    status_code=500,
                    content={
                        "error": {
                            "code": "TM_5002",
                            "message": "内部错误",
                            "details": {},
                            "trace_id": request_id,
                        }
                    },
                )
                response.headers["X-Request-ID"] = request_id
                return response

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
    async def readyz() -> Any:
        """就绪探针：检查数据库连通性。"""
        from tagmind.db.session import ping

        # ping() 会走 psycopg 同步连接；在 async 端点里直接调用会阻塞事件循环
        # 直到 connect_timeout（实测可达数百毫秒），期间整个服务无法处理请求。
        # 放到线程池，保持 async 签名与响应体不变。
        database_ok = await run_in_threadpool(ping)
        if not database_ok:
            # 就绪探针按 HTTP 状态码判定。此前 DB 已断仍返回 200，
            # K8s / LB 会把流量继续导向故障实例。
            return JSONResponse(
                status_code=503,
                content={"status": "degraded", "database": "down"},
            )
        return {"status": "ok", "database": "up"}

    return app
