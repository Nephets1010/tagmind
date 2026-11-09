"""核心横切能力：异常体系、结构化日志、请求上下文、安全原语。

这一层不含业务语义，被 API、仓储、调度与所有离线任务共同依赖。

- :mod:`tagmind.core.errors`   异常层级与错误码（架构 §14.3）。
- :mod:`tagmind.core.logging`  structlog JSON 日志与敏感字段脱敏（架构 §14.4）。
- :mod:`tagmind.core.context`  contextvar 承载 request_id / trace_id。
- :mod:`tagmind.core.security` API Key、PII 正则族、路径白名单、令牌桶限流。
"""

from __future__ import annotations

from tagmind.core.context import (
    bind_context,
    clear_context,
    current_context,
    new_request_id,
    new_trace_id,
)
from tagmind.core.errors import TagmindError
from tagmind.core.logging import configure_logging, get_logger

__all__ = [
    "TagmindError",
    "bind_context",
    "clear_context",
    "configure_logging",
    "current_context",
    "get_logger",
    "new_request_id",
    "new_trace_id",
]
