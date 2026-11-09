"""``content_vector`` 表映射（pgvector）。

对应架构文档 §5.7。

**向量口径**（歧义 A-05 的裁决）：``tag_vector``（JSONB）存**未归一化**的标签
向量，是标签向量的**真相源**；``tag_vector_norm`` 存 L2 模长。归一化在读取时
计算，避免存储冗余精度损失，也便于调参后重算。

``pgvector`` 是可选依赖（``pip install tagmind[pgvector]``）。未安装时
``Vector`` 类型不可用，这里用 ``TEXT`` 占位并保持模型可导入——这样不装
pgvector 的开发机仍能跑单元测试，只是不能建 HNSW 索引。
"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeEngine

from tagmind.db.base import Base, JSONBType, UTCDateTime

__all__ = ["VECTOR_AVAILABLE", "VECTOR_DIM", "ContentVector"]

#: 稠密向量维度（BGE-M3）。
VECTOR_DIM = 1024

try:  # pragma: no cover - 取决于本地是否装了 pgvector
    from pgvector.sqlalchemy import Vector

    VECTOR_AVAILABLE = True
except ImportError:  # pragma: no cover
    VECTOR_AVAILABLE = False


def _embedding_column() -> TypeEngine[object]:
    """构造 ``embedding`` 列类型。

    装了 pgvector 就用真 ``VECTOR(1024)``，没装就用 ``TEXT`` 占位，
    保证模块始终可导入（否则整个 ``db.models`` 包都不可用）。
    """
    if VECTOR_AVAILABLE:
        return cast("TypeEngine[object]", Vector(VECTOR_DIM))
    return cast("TypeEngine[object]", Text())


class ContentVector(Base):
    """内容向量表：稠密向量 + 标签稀疏向量。"""

    __tablename__ = "content_vector"

    content_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("content.id", ondelete="CASCADE"),
        primary_key=True,
    )
    embedding: Mapped[object] = mapped_column(_embedding_column(), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding_version: Mapped[str] = mapped_column(String(64), nullable=False)
    dim: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=VECTOR_DIM)
    norm: Mapped[float] = mapped_column(Numeric(12, 8), nullable=False, default=1)
    tag_vector: Mapped[dict] = mapped_column(JSONBType, nullable=False, default=dict)
    tag_vector_norm: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    tag_space_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default="now()"
    )

    __table_args__ = (
        CheckConstraint("dim = 1024", name="ck_content_vector_dim"),
        Index("ix_content_vector_tag", "tag_vector", postgresql_using="gin"),
    )
