"""Engine / SessionFactory / 连接池 / 健康探针。

架构契约（§14.5）：

- ``timezone=UTC`` 在**连接级**强制，避免容器时区影响「今天」的判定。
- ``pool_pre_ping=True``：容器化环境里数据库可能随时重启，探活不可省。
- 写路径用 :func:`session_scope`；只读快路径用独立的只读 Session，
  避免与写事务争用连接。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from tagmind.config.settings import Settings, get_settings
from tagmind.core.logging import get_logger

__all__ = [
    "build_engine",
    "get_engine",
    "get_readonly_session_factory",
    "get_session_factory",
    "ping",
    "session_scope",
]


def build_engine(settings: Settings | None = None, *, url: str | None = None) -> Engine:
    """创建 Engine。

    Args:
        settings: 配置对象；``None`` 时取全局单例。
        url: 显式连接串；``None`` 时用 ``settings.resolved_database_url``。

    Returns:
        配置好的 :class:`~sqlalchemy.Engine`。
    """
    resolved = settings or get_settings()
    dsn = url or resolved.resolved_database_url
    return create_engine(
        dsn,
        pool_size=resolved.db_pool_size,
        max_overflow=resolved.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
        connect_args={
            "options": "-c timezone=UTC",
            # 就绪探针必须在秒级给出结论。缺少 connect_timeout 时，
            # 对端不可达会让连接挂在操作系统的 TCP 重试窗口上（可达数分钟），
            # 表现为 /readyz 挂起而非快速返回 degraded。
            "connect_timeout": resolved.db_connect_timeout,
        },
    )


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """返回进程内单例 Engine。"""
    return build_engine()


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    """返回写路径的 Session 工厂。

    ``expire_on_commit=False``：提交后 ORM 对象仍可读取，避免在事务外
    访问属性时触发隐式刷新（那一刷会开新连接）。
    """
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


@lru_cache(maxsize=1)
def get_readonly_session_factory() -> sessionmaker[Session]:
    """返回只读快路径的 Session 工厂。

    与写路径分开，避免推荐链路与批处理写事务争用连接池。
    """
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """写事务边界。

    正常结束提交，异常回滚。这是**唯一**允许写库的上下文管理器——
    业务代码不应自己 ``session.commit()``。

    Yields:
        一个 :class:`~sqlalchemy.orm.Session`。
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ping(url: str | None = None) -> bool:
    """探活数据库。

    Args:
        url: 连接串；``None`` 时用全局 Engine。

    Returns:
        可连通返回 ``True``，否则返回 ``False``（不抛异常——健康检查
        需要的是布尔结论，不是异常）。
    """
    try:
        if url is None:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
        else:
            engine = build_engine(url=url)
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
            finally:
                engine.dispose()
    except Exception as exc:
        # 吞掉异常却不留日志时，/readyz 只能给出布尔值，无法区分认证失败、
        # 连接超时还是 DNS 问题。返回契约不变，只补可诊断性。
        get_logger("tagmind.db").warning(
            "db.ping.failed",
            error_type=type(exc).__name__,
            error=str(exc)[:200],
        )
        return False
    return True
