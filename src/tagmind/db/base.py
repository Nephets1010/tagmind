"""ORM 基类、公共列类型与命名约定。

命名约定直接对齐架构文档 §5.1：

======== =========================================
前缀     含义
======== =========================================
ix_      普通索引
ux_      唯一索引
ck_      检查约束
fk_      外键
pk_      主键
======== =========================================

统一命名的意义：运维在 10 万行表里排查问题时，能靠前缀判断索引类型，
不需要去翻建表语句。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, MetaData
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

__all__ = [
    "Base",
    "JSONBType",
    "UTCDateTime",
    "naming_convention",
]

#: 全局命名约定。所有未显式命名的约束都会落到这里。
naming_convention: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "ux_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """全部 ORM 模型的基类。

    ``metadata`` 集中管理所有表定义，``alembic`` 的 ``target_metadata``
    绑定到它，``scripts/consistency_check.py`` 也用它做结构比对。
    """

    metadata = MetaData(naming_convention=naming_convention)


class UTCDateTime(TypeDecorator[datetime]):
    """始终以 UTC 存储与返回的带时区时间戳。

    数据库连接层已经用 ``timezone=UTC`` 强制了会话时区（架构 §14.5），
    这里再做一层保证，是为了让 ORM 在被连到非 UTC 实例时也不会写歪。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:  # noqa: ARG002
        """写入前统一转成 UTC。

        Args:
            value: 待写入的时间值。
            dialect: SQLAlchemy 方言（本实现不依赖方言，跳过）。

        Returns:
            带 UTC 时区的时间值。
        """
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:  # noqa: ARG002
        """读出时确保带 UTC 时区。

        Args:
            value: 数据库返回的时间值。
            dialect: SQLAlchemy 方言（本实现不依赖方言，跳过）。

        Returns:
            带 UTC 时区的时间值。
        """
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class JSONBType(TypeDecorator[Any]):
    """JSONB 列类型。

    PostgreSQL 的 JSONB 会自动去重 key 并排序，读写性能优于 JSON；
    本项目所有结构化扩展列（``labels`` / ``tag_weights`` / ``clean_stats`` …）
    都用它。

    ``none_as_null=True`` 是**必需**的：SQLAlchemy 的 JSON 类型默认把 Python
    的 ``None`` 序列化成 JSON ``null``，而不是 SQL ``NULL``。这会让 §5.2 的
    ``CHECK (chunks IS NULL OR jsonb_typeof(chunks) = 'array')`` 之类的约束
    全部失败——``jsonb_typeof('null'::jsonb)`` 返回 ``'null'`` 而不是 ``'array'``。
    """

    impl = JSONB
    cache_ok = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """构造 JSONB 类型，强制 ``none_as_null=True``。"""
        kwargs.setdefault("none_as_null", True)
        super().__init__(*args, **kwargs)
