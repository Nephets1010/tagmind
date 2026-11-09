"""工作流表映射：``label_conflict_log`` / ``review_task`` / ``review_record`` / ``label_drift_check``。

对应架构文档 §5.4 与 §5.7。

``review_record`` 是**追加式**的复核轨迹：``UPDATE`` / ``DELETE`` 被数据库触发器
拒绝（``fn_forbid_mutation``）。这一约束在**数据库层**而非应用层实现——
应用层的「只 insert」约定挡不住一次误执行的 SQL。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from tagmind.db.base import Base, JSONBType, UTCDateTime

__all__ = ["LabelConflictLog", "LabelDriftCheck", "ReviewRecord", "ReviewTask"]


class LabelConflictLog(Base):
    """冲突与仲裁全量日志。"""

    __tablename__ = "label_conflict_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    content_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("content.id", ondelete="CASCADE"), nullable=False
    )
    candidates: Mapped[list] = mapped_column(JSONBType, nullable=False)
    resolved: Mapped[list | None] = mapped_column(JSONBType, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    conflict_type: Mapped[str] = mapped_column(String(24), nullable=False)
    detected_by: Mapped[str] = mapped_column(String(8), nullable=False)
    retrieved_evidence_ids: Mapped[list | None] = mapped_column(JSONBType, nullable=True)
    retrieved_scores: Mapped[dict | None] = mapped_column(JSONBType, nullable=True)
    arbitrator_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    arbitrator_prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    reviewer: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    resolved_before: Mapped[list | None] = mapped_column(JSONBType, nullable=True)
    # ↓↓↓ 扩展列：保证「冲突可追溯率 100%」可被机器校验 ↓↓↓
    rule_hits: Mapped[list] = mapped_column(JSONBType, nullable=False, default=list)
    evidence_detail: Mapped[list] = mapped_column(JSONBType, nullable=False, default=list)
    pipeline_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    label_def_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default="now()"
    )

    __table_args__ = (
        CheckConstraint("detected_by IN ('rule','llm','human')", name="ck_conflict_detected_by"),
        Index("ix_conflict_content", "content_id", "created_at"),
        Index("ix_conflict_review", "review_status", "created_at"),
        Index("ix_conflict_type", "conflict_type"),
        Index("ix_conflict_created", "created_at"),
        Index(
            "ix_conflict_candidates",
            "candidates",
            postgresql_using="gin",
            postgresql_ops={"candidates": "jsonb_path_ops"},
        ),
    )


class ReviewTask(Base):
    """人工复核任务。"""

    __tablename__ = "review_task"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False, default="content")
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=5)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    assignee: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict] = mapped_column(JSONBType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default="now()"
    )
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    done_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("task_type", "target_type", "target_id", name="ux_review_task_target"),
        CheckConstraint("task_type IN ('conflict','quality','drift')", name="ck_review_task_type"),
        CheckConstraint(
            "target_type IN ('content','conflict','sample')", name="ck_review_task_target_type"
        ),
        CheckConstraint(
            "status IN ('pending','claimed','done','cancelled')", name="ck_review_task_status"
        ),
        Index("ix_review_task_status", "status", "priority", "created_at"),
    )


class ReviewRecord(Base):
    """复核轨迹（**只增不改**）。

    ``UPDATE`` / ``DELETE`` 由数据库触发器 ``tg_review_record_immutable`` 拒绝。
    """

    __tablename__ = "review_record"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("review_task.id", ondelete="CASCADE"), nullable=False
    )
    content_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("content.id", ondelete="SET NULL"), nullable=True
    )
    reviewer: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    before_json: Mapped[dict | None] = mapped_column(JSONBType, nullable=True)
    after_json: Mapped[dict | None] = mapped_column(JSONBType, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default="now()"
    )

    __table_args__ = (
        CheckConstraint(
            "action IN ('approve','reject','override','modify')", name="ck_review_record_action"
        ),
        Index("ix_review_record_task", "task_id", "created_at"),
    )


class LabelDriftCheck(Base):
    """标签漂移周检记录。

    ``manual_filled`` 表示本轮是否发生了人工回填。Q20 边界：工程侧只做到
    「生成抽检表」，``manual_filled=false`` 时 ``consistency_rate`` 必须为 NULL——
    不得用模型自评代替人工一致率。
    """

    __tablename__ = "label_drift_check"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    check_week: Mapped[str] = mapped_column(String(10), nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    consistency_rate: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    drift_detected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    low_support_labels: Mapped[list] = mapped_column(JSONBType, nullable=False, default=list)
    fed_back_examples: Mapped[list] = mapped_column(JSONBType, nullable=False, default=list)
    reviewer: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pipeline_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    label_def_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sampling_method: Mapped[str] = mapped_column(
        String(32), nullable=False, default="random_stratified_l1"
    )
    manual_filled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default="now()"
    )

    __table_args__ = (UniqueConstraint("check_week", name="ux_drift_week"),)
