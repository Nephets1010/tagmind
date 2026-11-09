"""持久化层：ORM 基类、会话工厂与仓储。

**依赖红线**（架构文档 §3 / §14.5）：

- 业务模块**不得**直接连库，一律经 :mod:`tagmind.db.repositories`。
- 一个仓储方法 = 一个事务；跨仓储的业务操作由编排层显式包裹事务。
- LLM / HTTP 调用必须在事务**外**，否则长事务锁行、连接池耗尽。
"""

from __future__ import annotations

from tagmind.db.base import Base, naming_convention
from tagmind.db.session import (
    build_engine,
    get_engine,
    get_session_factory,
    ping,
    session_scope,
)

__all__ = [
    "Base",
    "build_engine",
    "get_engine",
    "get_session_factory",
    "naming_convention",
    "ping",
    "session_scope",
]
