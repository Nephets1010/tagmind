"""Alembic 迁移的公共工具。

**为什么需要 ``execute_raw``**：本项目的 DDL 里含 JSON 字面量默认值，例如
``DEFAULT '{"control":0.5,"treatment":0.5}'::jsonb``。psycopg3 的
``paramstyle`` 是 ``pyformat``，``%`` 会被当成绑定参数占位符——
``sa.text()`` 因此把 ``0.5`` 里的 ``%``（转义后的 ``%(0)s``）误认成参数名，
报 ``A value is required for bind parameter '0'``。

改用 ``exec_driver_sql`` 走驱动原生路径，不做任何 ``%`` 转义。
"""

from __future__ import annotations

from alembic import op

__all__ = ["execute_raw"]


def execute_raw(sql: str) -> None:
    """执行一段原始 SQL。

    与 :func:`alembic.op.execute` 的区别：不走 ``sa.text()`` 的绑定参数解析，
    因此 SQL 里的 ``%`` 会被原样送到数据库。

    Args:
        sql: 要执行的 SQL 文本（可含多条以 ``;`` 分隔的语句）。
    """
    op.get_bind().exec_driver_sql(sql)
