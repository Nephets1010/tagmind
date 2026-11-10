"""API 可观测性回归测试。

锁定架构审查中 4 项低风险修复，防止回归：

* A-12 ``context_scope`` 多字段绑定退出时全部恢复（含异常路径）；
* A-02 HTTP 中间件真正把 request_id 绑进 contextvar；
* A-13 未捕获异常路径仍有访问日志所需的 500 响应与 ``X-Request-ID`` 头；
* A-01 ``/readyz`` 的同步 DB 探测经线程池，不阻塞事件循环；
* A-14 结构化日志同时提供契约字段 ``ts`` 与兼容字段 ``timestamp``。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tagmind.api.app import create_app
from tagmind.core.context import context_scope, current_context
from tagmind.core.logging import _add_ts_alias


class TestContextScope:
    """A-12：多字段上下文必须完整恢复。"""

    def test_all_fields_restored_after_normal_exit(self) -> None:
        with context_scope(request_id="r-1", trace_id="t-1", user_id="u-1", content_id=42) as ctx:
            assert ctx.request_id == "r-1"
            assert ctx.trace_id == "t-1"
            assert ctx.user_id == "u-1"
            assert ctx.content_id == 42
        assert current_context() is None, "退出 scope 后上下文发生泄漏"

    def test_all_fields_restored_after_exception(self) -> None:
        with pytest.raises(RuntimeError, match="boom"):
            with context_scope(request_id="r-2", trace_id="t-2", user_id="u-2", content_id=7):
                raise RuntimeError("boom")
        assert current_context() is None, "异常退出 scope 后上下文发生泄漏"

    def test_adjacent_scopes_do_not_leak(self) -> None:
        """复用线程时，上一条请求的 ID 不得串到下一条。"""
        with context_scope(request_id="first", trace_id="first", user_id="user-a"):
            pass
        with context_scope(request_id="second", trace_id="second") as ctx:
            assert ctx.request_id == "second"
            assert ctx.user_id is None, "上一条请求的 user_id 泄漏了"


class TestMiddleware:
    """A-02 / A-13：中间件绑定上下文并覆盖异常路径。"""

    @staticmethod
    def _app_with_probe() -> FastAPI:
        app = create_app()

        @app.get("/_test/context")
        async def _probe() -> dict[str, object]:
            ctx = current_context()
            return {
                "request_id": ctx.request_id if ctx else None,
                "trace_id": ctx.trace_id if ctx else None,
            }

        @app.get("/_test/boom")
        async def _boom() -> None:
            raise RuntimeError("unhandled")

        return app

    def test_context_is_bound_during_request(self) -> None:
        with TestClient(self._app_with_probe()) as client:
            response = client.get("/_test/context", headers={"X-Request-ID": "trace-xyz"})
        body = response.json()
        assert body["request_id"] == "trace-xyz"
        assert body["trace_id"] == "trace-xyz"

    def test_response_carries_request_id(self) -> None:
        with TestClient(create_app()) as client:
            response = client.get("/healthz", headers={"X-Request-ID": "trace-abc"})
        assert response.headers["X-Request-ID"] == "trace-abc"

    def test_unhandled_exception_returns_structured_500_with_trace_header(self) -> None:
        """此前异常路径既不记访问日志、也没有 X-Request-ID 头。"""
        with TestClient(self._app_with_probe(), raise_server_exceptions=False) as client:
            response = client.get("/_test/boom", headers={"X-Request-ID": "trace-err"})

        assert response.status_code == 500
        assert response.headers["X-Request-ID"] == "trace-err"
        body = response.json()
        assert body["error"]["code"] == "TM_5002"
        assert body["error"]["trace_id"] == "trace-err"


class TestReadyzDoesNotBlockEventLoop:
    """A-01：同步 ping 必须经线程池调用。"""

    def test_ping_is_dispatched_to_threadpool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tagmind.api.app as app_module
        import tagmind.db.session as session_module

        dispatched: list[str] = []

        async def fake_threadpool(func, *args, **kwargs):  # type: ignore[no-untyped-def]
            dispatched.append("threadpool")
            return func(*args, **kwargs)

        monkeypatch.setattr(app_module, "run_in_threadpool", fake_threadpool)
        monkeypatch.setattr(session_module, "ping", lambda *a, **k: False)
        # app.py 在端点内部延迟 import ping，这里 patch 的是模块属性，
        # 端点拿到的就是被替换后的函数。
        with TestClient(create_app()) as client:
            response = client.get("/readyz")

        assert response.status_code == 503
        assert response.json()["database"] == "down"
        assert dispatched == ["threadpool"], "ping 未走线程池，会阻塞事件循环"


class TestLogTimestampAlias:
    """A-14：契约字段 ts 与兼容字段 timestamp 必须同值共存。"""

    def test_ts_alias_is_added(self) -> None:
        event: dict[str, object] = {"timestamp": "2026-01-01T00:00:00Z", "event": "x"}
        result = _add_ts_alias(None, "info", event)  # type: ignore[arg-type]
        assert result["ts"] == "2026-01-01T00:00:00Z"
        assert result["timestamp"] == "2026-01-01T00:00:00Z"

    def test_existing_ts_is_not_overwritten(self) -> None:
        event: dict[str, object] = {"timestamp": "old", "ts": "explicit"}
        result = _add_ts_alias(None, "info", event)  # type: ignore[arg-type]
        assert result["ts"] == "explicit"

    def test_missing_timestamp_is_tolerated(self) -> None:
        event: dict[str, object] = {"event": "x"}
        result = _add_ts_alias(None, "info", event)  # type: ignore[arg-type]
        assert "ts" not in result
