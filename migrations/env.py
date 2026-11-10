"""Alembic 环境：URL 注入、``target_metadata`` 绑定、离线/在线模式。

**URL 来源**：优先读 ``TAGMIND_DATABASE_URL`` / ``Settings``，
其次读 ``alembic.ini`` 的 ``sqlalchemy.url``。这样容器里可以用环境变量
覆盖连接串，而本地开发仍能用 ini 里的默认值。
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 让 ``alembic`` 能 import 到 ``tagmind`` 包（未安装为 wheel 时的源码布局）。
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from tagmind.db.base import Base
from tagmind.db.models import *  # noqa: F403 - 导入全部模型以填充 metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

#: 迁移目标元数据。绑定 ORM 的 metadata，autogenerate 才能看到全部 16 张表。
target_metadata = Base.metadata


def _resolve_url() -> str:
    """解析数据库连接串。

    优先级：``TAGMIND_DATABASE_URL`` 环境变量 → ``Settings`` 派生值 →
    ``alembic.ini`` 中的 ``sqlalchemy.url``。

    Returns:
        SQLAlchemy 连接串。
    """
    env_url = os.getenv("TAGMIND_DATABASE_URL", "").strip()
    if env_url:
        return env_url
    try:
        from tagmind.config.settings import get_settings

        settings = get_settings()
        if settings.database_url:
            return settings.database_url
        return settings.resolved_database_url
    except Exception as exc:
        # 此前裸 except 把「production 缺密码」这类 ConfigError 一起吞掉，
        # 最终只报「未找到数据库连接串」，把真实原因掩盖了。
        from tagmind.core.errors import ConfigError

        if isinstance(exc, ConfigError):
            raise RuntimeError(f"数据库配置校验失败：{exc}") from exc
        return config.get_main_option("sqlalchemy.url", "")


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL 脚本，不连库。"""
    url = _resolve_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行迁移。"""
    url = _resolve_url()
    if not url:
        raise RuntimeError(
            "未找到数据库连接串：请设置 TAGMIND_DATABASE_URL 或在 alembic.ini 中配置"
        )
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = url
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
