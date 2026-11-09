"""API 应用工厂与 Celery 应用配置的契约测试。

覆盖 ``api/app.py`` 的中间件、异常处理与健康探针，以及
``scheduler/celery_app.py`` 的队列路由与重试策略。

这两处是「运行时契约」：路径名、状态码、请求头名一旦漂移，
Docker healthcheck、compose 探针与前端监控会同时失效，
因此用测试把契约钉死。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tagmind.api.app import create_app
from tagmind.core.errors import AuthError, NotFoundError


class TestCreateApp:
    """应用工厂的基本契约。"""

    def test_returns_fastapi_instance(self) -> None:
        assert isinstance(create_app(), FastAPI)

    def test_title_and_version(self) -> None:
        app = create_app()
        assert app.title == "tagmind"
        assert app.version

    def test_openapi_and_docs_enabled(self) -> None:
        app = create_app()
        assert app.openapi_url == "/openapi.json"
        assert app.docs_url == "/docs"

    def test_accepts_injected_settings(self) -> None:
        from tagmind.config.settings import Settings

        app = create_app(Settings(log_level="DEBUG"))
        assert app is not None


class TestHealthProbes:
    """探针路径是既有契约：Dockerfile 与 compose 都指向它们。"""

    def test_healthz_returns_ok(self) -> None:
        with TestClient(create_app()) as client:
            response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_readyz_reports_database_state(self) -> None:
        """就绪探针必须始终返回可解析的 database 字段。"""
        with TestClient(create_app()) as client:
            response = client.get("/readyz")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] in {"ok", "degraded"}
        assert body["database"] in {"up", "down"}


class TestRequestContextMiddleware:
    """request_id 必须贯穿请求并回写到响应头。"""

    def test_response_carries_request_id_header(self) -> None:
        with TestClient(create_app()) as client:
            response = client.get("/healthz")
        assert "X-Request-ID" in response.headers
        assert response.headers["X-Request-ID"]

    def test_inbound_request_id_is_reused(self) -> None:
        """客户端提供的 request_id 必须被沿用，便于跨服务串联。"""
        with TestClient(create_app()) as client:
            response = client.get("/healthz", headers={"X-Request-ID": "trace-abc-123"})
        assert response.headers["X-Request-ID"] == "trace-abc-123"

    def test_generated_request_id_is_unique_per_request(self) -> None:
        with TestClient(create_app()) as client:
            first = client.get("/healthz").headers["X-Request-ID"]
            second = client.get("/healthz").headers["X-Request-ID"]
        assert first != second


class TestErrorHandler:
    """领域异常必须渲染为统一错误体。"""

    def test_domain_error_returns_structured_payload(self) -> None:
        app = create_app()

        @app.get("/_test/boom")
        async def _boom() -> None:
            raise NotFoundError("内容不存在", details={"content_id": "c-1"})

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/_test/boom")

        assert response.status_code == 404
        body = response.json()
        # 统一错误体契约（架构 §14.3）：{"error": {code, message, details, trace_id}}
        error = body["error"]
        assert error["code"]
        assert error["message"]
        assert error["details"]["content_id"] == "c-1"
        assert "trace_id" in error

    def test_auth_error_maps_to_401(self) -> None:
        app = create_app()

        @app.get("/_test/auth")
        async def _auth() -> None:
            raise AuthError("缺少 API Key", details={"reason": "missing_api_key"})

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/_test/auth")

        assert response.status_code == 401
        assert response.json()["error"]["details"]["reason"] == "missing_api_key"


class TestCeleryApp:
    """Celery 应用的队列路由与幂等配置。"""

    def test_app_name(self) -> None:
        from tagmind.scheduler.celery_app import celery_app

        assert celery_app.main == "tagmind"

    def test_task_routing_separates_queues(self) -> None:
        """打标、画像、维护任务必须走独立队列，避免互相饿死。"""
        from tagmind.scheduler.celery_app import celery_app

        routes = celery_app.conf.task_routes
        assert routes["tagmind.label.*"]["queue"] == "label"
        assert routes["tagmind.profile.*"]["queue"] == "profile"
        assert routes["tagmind.maintenance.*"]["queue"] == "maintenance"

    def test_late_acknowledgement_enabled(self) -> None:
        """acks_late 保证 worker 崩溃时任务重回队列（ADR-0009）。"""
        from tagmind.scheduler.celery_app import celery_app

        assert celery_app.conf.task_acks_late is True
        assert celery_app.conf.task_reject_on_worker_lost is True

    def test_prefetch_multiplier_is_one(self) -> None:
        """prefetch=1 避免长任务饿死短任务。"""
        from tagmind.scheduler.celery_app import celery_app

        assert celery_app.conf.worker_prefetch_multiplier == 1

    def test_utc_enabled(self) -> None:
        """所有时间戳统一 UTC（PRD §6 设计原则 3）。"""
        from tagmind.scheduler.celery_app import celery_app

        assert celery_app.conf.enable_utc is True

    def test_time_limits_configured(self) -> None:
        """必须设置硬超时，避免任务永久挂起。"""
        from tagmind.scheduler.celery_app import celery_app

        assert celery_app.conf.task_time_limit > 0
        assert celery_app.conf.task_soft_time_limit < celery_app.conf.task_time_limit

    def test_broker_retry_on_startup_enabled(self) -> None:
        """broker 启动时必须允许重试：Redis 常常晚于 worker 就绪。"""
        from tagmind.scheduler.celery_app import celery_app

        assert celery_app.conf.broker_connection_retry_on_startup is True

    def test_broker_transport_options_configured(self) -> None:
        """传输层重试参数必须显式给出，避免依赖库默认值。"""
        from tagmind.scheduler.celery_app import celery_app

        options = celery_app.conf.broker_transport_options or {}
        assert options, "必须显式配置 broker 传输选项"
