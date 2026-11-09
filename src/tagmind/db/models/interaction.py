"""交互表映射：``user_profile`` / ``exposure_log``（按月分区）。

对应架构文档 §5.5 与 §5.6。

**分区约束**（歧义 A-04 的裁决）：PostgreSQL 的分区表要求唯一约束**必须包含
分区键**，因此主键是 ``PRIMARY KEY (id, ts)`` 而不是 ``PRIMARY KEY (id)``；
``request_id`` 的唯一性由 ``ux_exposure_idem (request_id, content_id, action, ts)``
承担。

**双权重字段**（歧义 A-02 的裁决）：``tag_weights`` 存归一化后的非负权重
（和 = 1），``label_affinity`` 存带符号原始分（可负）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from tagmind.db.base import Base, JSONBType, UTCDateTime

__all__ = ["ExposureLog", "UserProfile"]


class UserProfile(Base):
    """用户标签权重画像。"""

    __tablename__ = "user_profile"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tag_weights: Mapped[dict] = mapped_column(JSONBType, nullable=False, default=dict)
    cold_start: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False, default=0)
    behavior_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    label_dims: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    decay_lambda: Mapped[float] = mapped_column(Numeric(8, 6), nullable=False, default=0.95)
    last_behavior_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    drift_detected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    drift_score: Mapped[float | None] = mapped_column(Numeric(8, 6), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default="now()"
    )
    # ↓↓↓ 扩展列 ↓↓↓
    label_affinity: Mapped[dict] = mapped_column(JSONBType, nullable=False, default=dict)
    label_weight_norm: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    tag_space_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cold_start_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    weights_sum: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        CheckConstraint("behavior_count >= 0", name="ck_profile_behavior_count"),
        CheckConstraint("label_dims >= 0", name="ck_profile_label_dims"),
        CheckConstraint("decay_lambda > 0 AND decay_lambda <= 1", name="ck_profile_decay_lambda"),
        CheckConstraint(
            "drift_score IS NULL OR (drift_score >= 0 AND drift_score <= 1)",
            name="ck_profile_drift_score",
        ),
        Index(
            "ix_profile_tag_weights",
            "tag_weights",
            postgresql_using="gin",
            postgresql_ops={"tag_weights": "jsonb_path_ops"},
        ),
        Index("ix_profile_cold_start", "cold_start", postgresql_where="cold_start"),
        Index("ix_profile_updated", "updated_at"),
        Index("ix_profile_drift", "drift_detected", postgresql_where="drift_detected"),
    )


class ExposureLog(Base):
    """曝光与行为流水（按月 RANGE 分区）。

    ``__table_args__`` 末项的 ``postgresql_partition_by`` 让 Alembic 生成的 DDL
    带 ``PARTITION BY RANGE (ts)``。
    """

    __tablename__ = "exposure_log"

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(), primary_key=True, nullable=False, autoincrement=True
    )
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    content_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("content.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    # ts 是分区键，必须参与主键——PostgreSQL 要求分区表的唯一约束包含分区键
    # （歧义 A-04 的裁决）。
    ts: Mapped[datetime] = mapped_column(UTCDateTime, primary_key=True, nullable=False)
    dwell_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    rank_pos: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    score: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    algo: Mapped[str | None] = mapped_column(String(32), nullable=True)
    recall_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    exp_group: Mapped[str] = mapped_column(String(32), nullable=False, default="default")
    request_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pipeline_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default="now()"
    )

    __table_args__ = (
        # 幂等：同一请求内同一内容同一动作只落一行。
        UniqueConstraint("request_id", "content_id", "action", "ts", name="ux_exposure_idem"),
        CheckConstraint("dwell_ms IS NULL OR dwell_ms >= 0", name="ck_exposure_dwell_ms"),
        CheckConstraint("rank_pos IS NULL OR rank_pos >= 1", name="ck_exposure_rank_pos"),
        CheckConstraint("action IN ('view','click','skip','dwell')", name="ck_exposure_action"),
        Index("ix_exposure_user_ts", "user_id", "ts"),
        Index("ix_exposure_content", "content_id"),
        Index("ix_exposure_group_ts", "exp_group", "ts"),
        Index("ix_exposure_request", "request_id"),
        Index("ix_exposure_action_ts", "action", "ts"),
        Index("ix_exposure_recall_src", "recall_source", "ts"),
        {"postgresql_partition_by": "RANGE (ts)"},
    )
