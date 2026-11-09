"""请求上下文：contextvar 承载 request_id / trace_id / user_id / content_id。

架构契约（架构文档 §14.4）：

- HTTP：中间件生成 ``request_id``（``uuid4().hex``），写入 contextvar → 绑定
  structlog → 响应头 ``X-Request-ID`` → ``exposure_log.request_id``。
- Celery：``trace_id`` 在提交时生成并透传任务参数，worker 全程携带；
  ``request_id`` 在 Celery 场景退化为 ``trace_id``。

用 contextvar 而非 threading.local，是为了让上下文在 async 任务里同样正确隔离。
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "RequestContext",
    "bind_context",
    "clear_context",
    "current_context",
    "new_request_id",
    "new_trace_id",
    "utcnow",
]

_request_id: ContextVar[str | None] = ContextVar("tagmind_request_id", default=None)
_trace_id: ContextVar[str | None] = ContextVar("tagmind_trace_id", default=None)
_user_id: ContextVar[str | None] = ContextVar("tagmind_user_id", default=None)
_content_id: ContextVar[int | None] = ContextVar("tagmind_content_id", default=None)


def utcnow() -> datetime:
    """当前 UTC 时间（带时区）。

    全系统统一用带时区的 UTC 时间，避免容器本地时区把「今天」算错一天。
    """
    return datetime.now(UTC)


def new_request_id() -> str:
    """生成一个新的请求 ID（32 位十六进制，无连字符）。"""
    return uuid.uuid4().hex


def new_trace_id() -> str:
    """生成一个新的全链路 ID。

    Celery 场景下它会被写进任务参数并随 worker 传播。
    """
    return uuid.uuid4().hex


@dataclass(frozen=True, slots=True)
class RequestContext:
    """一次请求/任务的上下文快照。"""

    request_id: str
    trace_id: str
    user_id: str | None = None
    content_id: int | None = None

    def to_log_fields(self) -> dict[str, str | int]:
        """转成 structlog 绑定字段。

        ``user_id`` 原始值由日志层的脱敏处理器负责哈希，这里只负责传递。
        """
        fields: dict[str, str | int] = {
            "request_id": self.request_id,
            "trace_id": self.trace_id,
        }
        if self.user_id is not None:
            fields["user_id"] = self.user_id
        if self.content_id is not None:
            fields["content_id"] = self.content_id
        return fields


def bind_context(
    *,
    request_id: str | None = None,
    trace_id: str | None = None,
    user_id: str | None = None,
    content_id: int | None = None,
) -> Token[Any] | None:
    """绑定上下文变量。

    Args:
        request_id: HTTP 请求 ID。
        trace_id: 全链路 ID。
        user_id: 用户 ID。
        content_id: 内容 ID。

    Returns:
        可用于 :func:`contextvars.ContextVar.reset` 的 token；无字段被设置时返回 ``None``。
    """
    token: Token[Any] | None = None
    if request_id is not None:
        token = _request_id.set(request_id)
    if trace_id is not None:
        trace_token = _trace_id.set(trace_id)
        token = token if token is not None else trace_token
    if user_id is not None:
        user_token = _user_id.set(user_id)
        token = token if token is not None else user_token
    if content_id is not None:
        content_token = _content_id.set(content_id)
        token = token if token is not None else content_token
    return token


def clear_context() -> None:
    """清空全部上下文变量。

    必须在线程池 / Celery worker 复用线程时调用，否则上一条任务的
    ``request_id`` 会污染下一条任务的日志。
    """
    _request_id.set(None)
    _trace_id.set(None)
    _user_id.set(None)
    _content_id.set(None)


def current_context() -> RequestContext | None:
    """取当前上下文快照。

    Returns:
        上下文对象；两个 ID 都未设置时返回 ``None``。
    """
    request_id = _request_id.get()
    trace_id = _trace_id.get()
    if request_id is None and trace_id is None:
        return None
    return RequestContext(
        request_id=request_id or trace_id or new_request_id(),
        trace_id=trace_id or request_id or new_trace_id(),
        user_id=_user_id.get(),
        content_id=_content_id.get(),
    )
