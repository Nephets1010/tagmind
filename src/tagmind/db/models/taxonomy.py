"""``label_def`` 表映射。

对应架构文档 §5.3 的完整 DDL。

**版本化语义**（歧义 A-01 的裁决）：``code`` 跨版本不变，因此不能全局 UNIQUE，
只能 ``UNIQUE(version, code)`` + ``UNIQUE(version, path)``，另建
``ix_label_def_code`` 支持跨版本按 code 定位。

**三写结构**（ADR-0008）：``parent`` 是真相源，``path`` / ``path_text`` /
``ancestors`` / ``path_prefix`` / ``root_code`` / ``depth`` 是派生索引。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from tagmind.db.base import Base, JSONBType, UTCDateTime

__all__ = ["LabelDef"]


class LabelDef(Base):
    """标签定义表（12 层体系）。"""

    __tablename__ = "label_def"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    layer: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    examples: Mapped[list] = mapped_column(JSONBType, nullable=False, default=list)
    parent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    max_select: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    exclusive_group: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sibling_exclusion: Mapped[list] = mapped_column(JSONBType, nullable=False, default=list)
    is_feature: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    # ↓↓↓ 扩展列（架构 §5.9） ↓↓↓
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    path_text: Mapped[str] = mapped_column(Text, nullable=False)
    depth: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    ancestors: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    root_code: Mapped[str] = mapped_column(String(64), nullable=False)
    df_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    idf: Mapped[float | None] = mapped_column(Float, nullable=True)
    path_prefix: Mapped[str] = mapped_column(String(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default="now()"
    )

    __table_args__ = (
        UniqueConstraint("version", "code", name="ux_label_def_version_code"),
        UniqueConstraint("version", "path", name="ux_label_def_version_path"),
        CheckConstraint("layer BETWEEN 1 AND 12", name="ck_label_def_layer_range"),
        CheckConstraint(
            "(layer = 1 AND parent IS NULL) OR (layer > 1 AND parent IS NOT NULL)",
            name="ck_label_def_layer_parent",
        ),
        CheckConstraint("depth = layer", name="ck_label_def_depth"),
        CheckConstraint("max_select <= 8", name="ck_label_def_max_select"),
        CheckConstraint("status IN ('active','deprecated')", name="ck_label_def_status"),
        Index("ix_label_def_version_layer", "version", "layer"),
        Index("ix_label_def_parent", "version", "parent"),
        Index(
            "ix_label_def_path_prefix",
            "version",
            "path_prefix",
            postgresql_ops={"path_prefix": "text_pattern_ops"},
        ),
        Index("ix_label_def_ancestors", "ancestors", postgresql_using="gin"),
        Index("ix_label_def_code", "code"),
        Index(
            "ix_label_def_exclusive",
            "exclusive_group",
            postgresql_where="exclusive_group IS NOT NULL",
        ),
        Index(
            "ix_label_def_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )
