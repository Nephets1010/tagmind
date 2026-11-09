"""启用扩展、创建公共域与公共触发器函数。

Revision ID: 0001_extensions_and_types
Revises:
Create Date: 2026-11-08

对应架构文档 §5.1「扩展与公共约定」。

**关于 pgvector**：``vector`` 扩展是 ADR-0001 选定的生产方案，但它不是
PostgreSQL 内置扩展。如果目标库没有装 pgvector，本迁移会退化为
``REAL[]`` 存储稠密向量（功能可用，但**没有 HNSW 索引**，检索退化为全表扫描）。

之所以不直接失败：CI 与本地开发环境未必装 pgvector，而「迁移能跑通」
比「索引类型完美」更重要。生产环境应使用 ``pgvector/pgvector:pg16`` 镜像，
此时 HNSW 索引与 ``VECTOR(1024)`` 类型都会生效——降级路径不会启用。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Alembic 不把 migrations/ 目录放进 sys.path，因此这里显式补上——
# 否则 `from helpers import ...` 在 CI 与本地都会 ModuleNotFoundError。
import sys
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))

from helpers import execute_raw  # noqa: E402

revision: str = "0001_extensions_and_types"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: 公共域定义。名称与类型与架构 §5.1 逐字一致。
#:
#: 注意：PostgreSQL 的 ``CREATE DOMAIN`` **不支持** ``NOT NULL`` 约束——
#: ``NOT NULL`` 只能加在**列**上。因此 ``ts_tz`` 只约束类型，实际的非空保证
#: 来自各表的列级 ``NOT NULL``（§5.2 的 DDL 里就是这么写的）。
_DOMAINS: tuple[tuple[str, str], ...] = (
    ("ts_tz", "TIMESTAMPTZ"),
    ("quality_score_t", "NUMERIC(3,1) CHECK (VALUE >= 0 AND VALUE <= 10)"),
    ("confidence_t", "NUMERIC(4,3) CHECK (VALUE >= 0 AND VALUE <= 1)"),
    (
        "content_status_t",
        "VARCHAR(16) CHECK (VALUE IN "
        "('pending','processing','approved','rejected','needs_review','archived'))",
    ),
    ("quality_status_t", "VARCHAR(16) CHECK (VALUE IN ('approved','rejected','needs_review'))"),
    (
        "conflict_type_t",
        "VARCHAR(24) CHECK (VALUE IN ('parent','mutual','exclusive','semantic_candidate'))",
    ),
    ("review_status_t", "VARCHAR(16) CHECK (VALUE IN ('pending','approved','overridden','auto'))"),
    ("action_t", "VARCHAR(8) CHECK (VALUE IN ('view','click','skip','dwell'))"),
)


def _has_vector_extension(conn: sa.Connection) -> bool:
    """判断当前库是否已启用 ``vector`` 扩展。"""
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        ).scalar()
    )


def _vector_available(conn: sa.Connection) -> bool:
    """判断当前库**是否可安装** ``vector`` 扩展。

    查 ``pg_available_extensions`` 而不是直接 ``CREATE EXTENSION`` 再捕获异常：
    后者一旦失败，PostgreSQL 会把整个事务置为 aborted 状态，``rollback()``
    会连带撤销 Alembic 自己创建的 ``alembic_version`` 表——迁移就再也记不上账了。
    """
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM pg_available_extensions WHERE name = 'vector' "
                "AND installed_version IS NULL"
            )
        ).scalar()
    )


def upgrade() -> None:
    """启用扩展并创建公共域与触发器函数。"""
    conn = op.get_bind()

    # --- 扩展 -----------------------------------------------------------
    # pg_trgm 用于标签名/内容的模糊检索；btree_gin 用于 JSONB + 标量复合索引。
    for extension in ("pg_trgm", "btree_gin"):
        op.execute(sa.text(f"CREATE EXTENSION IF NOT EXISTS {extension}"))
    # vector 扩展可能不存在（未装 pgvector 的库）——此时稠密向量降级为 REAL[]，
    # 功能可用但没有 HNSW 索引。生产用 pgvector/pgvector:pg16 镜像即完整能力。
    if _vector_available(conn):
        op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))

    # --- 公共域 ---------------------------------------------------------
    # PostgreSQL 不支持 ``CREATE DOMAIN IF NOT EXISTS``，因此先查 pg_type
    # 再决定是否创建——这也让迁移可重复执行。
    for name, definition in _DOMAINS:
        exists = op.get_bind().execute(
            sa.text(
                "SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE n.nspname = current_schema() AND t.typname = :name"
            ),
            {"name": name},
        ).scalar()
        if exists:
            continue
        op.execute(sa.text(f"CREATE DOMAIN {name} AS {definition}"))

    # --- 公共函数 -------------------------------------------------------
    # ``fn_set_updated_at`` 用 ``clock_timestamp()`` 而不是 ``now()``：
    # ``now()`` 返回**事务开始时间**，同一事务里的多次 UPDATE 会得到同一个值，
    # 乐观锁就检测不到并发修改。``clock_timestamp()`` 是真实时钟。
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION fn_set_updated_at() RETURNS TRIGGER
            LANGUAGE plpgsql AS $$
            BEGIN NEW.updated_at := clock_timestamp(); RETURN NEW; END $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION fn_forbid_mutation() RETURNS TRIGGER
            LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'table % is append-only (audit trail)', TG_TABLE_NAME;
            END $$
            """
        )
    )


def downgrade() -> None:
    """删除公共域与函数。

    **不删除扩展**——扩展可能被同库的其他 schema 共用，删除会造成不可逆影响。
    """
    for name, _ in reversed(_DOMAINS):
        op.execute(sa.text(f"DROP DOMAIN IF EXISTS {name} CASCADE"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS fn_forbid_mutation()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS fn_set_updated_at()"))
