"""``content`` 表映射。

对应架构文档 §5.2 的完整 DDL。字段名、类型、可空性与 DDL 逐项一致——
``scripts/consistency_check.py`` 的 V14 校验就靠这个一致性。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from tagmind.db.base import Base, JSONBType, UTCDateTime

__all__ = ["Content", "PiiMaskMap"]


class Content(Base):
    """内容主表。

    ``raw_text`` 是未脱敏的原始正文（受控访问），``clean_text`` 是清洗 + 脱敏
    后的正文。两者分开存，是为了让「送 LLM 的内容一定脱敏过」这条不变量
    在数据层可验证。
    """

    __tablename__ = "content"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    clean_text: Mapped[str] = mapped_column(Text, nullable=False)
    labels: Mapped[list] = mapped_column(JSONBType, nullable=False, default=list)
    label_raw: Mapped[dict | None] = mapped_column(JSONBType, nullable=True)
    label_confidence: Mapped[dict | None] = mapped_column(JSONBType, nullable=True)
    label_stats: Mapped[dict | None] = mapped_column(JSONBType, nullable=True)
    labels_version: Mapped[str] = mapped_column(String(64), nullable=False)
    quality_score: Mapped[float | None] = mapped_column(Numeric(3, 1), nullable=True)
    quality_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality_status: Mapped[str] = mapped_column(String(16), nullable=False, default="needs_review")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    reject_reason: Mapped[dict | None] = mapped_column(JSONBType, nullable=True)
    clean_stats: Mapped[dict | None] = mapped_column(JSONBType, nullable=True)
    chunks: Mapped[list | None] = mapped_column(JSONBType, nullable=True)
    pii_masked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pii_fields: Mapped[list | None] = mapped_column(JSONBType, nullable=True)
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedding_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pipeline_version: Mapped[str] = mapped_column(String(128), nullable=False)
    # ↓↓↓ 扩展列（架构 §5.9） ↓↓↓
    label_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False, default=list)
    layer_label_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), nullable=False, default=list
    )
    l1_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    l2_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    media_refs: Mapped[list] = mapped_column(JSONBType, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default="now()"
    )
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="ux_content_idempotency"),
        CheckConstraint("length(clean_text) >= 1", name="ck_content_clean_nonempty"),
        CheckConstraint(
            "(status = 'approved' AND published_at IS NOT NULL) OR status <> 'approved'",
            name="ck_content_published",
        ),
        Index("ix_content_label_ids", "label_ids", postgresql_using="gin"),
        Index("ix_content_layer_label_ids", "layer_label_ids", postgresql_using="gin"),
        Index("ix_content_status_qscore", "status", "quality_score"),
        Index("ix_content_created", "created_at"),
        Index("ix_content_hash", "content_hash"),
        Index("ix_content_labels_version", "labels_version"),
        Index(
            "ix_content_dispatchable",
            "published_at",
            postgresql_where="status = 'approved' AND quality_status = 'approved'",
        ),
    )


class PiiMaskMap(Base):
    """PII 脱敏映射表。

    **只存哈希，不存原值**（架构 §5.7）：``original_hash`` 是 sha256(原值)，
    无法反推原文。表级只读保护（``REVOKE ALL ON pii_mask_map FROM PUBLIC``）
    由迁移脚本落库。

    该表的访问受 NF-S02 约束，需鉴权；因此不在常规仓储的公开接口里暴露原文。
    """

    __tablename__ = "pii_mask_map"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    content_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("content.id", ondelete="CASCADE"), nullable=False
    )
    placeholder: Mapped[str] = mapped_column(String(32), nullable=False)
    pii_type: Mapped[str] = mapped_column(String(16), nullable=False)
    original_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default="now()"
    )

    __table_args__ = (
        UniqueConstraint("content_id", "placeholder", name="ux_pii_placeholder"),
        CheckConstraint(
            "pii_type IN ('phone','id_card','email','bank_card','name','address')",
            name="ck_pii_type",
        ),
        Index("ix_pii_content", "content_id"),
    )
