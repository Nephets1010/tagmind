"""审计与实验表映射。

对应架构文档 §5.7：

- ``llm_call_audit``：LLM 调用审计，``prompt_hash`` 可还原。
- ``egress_log``：外发请求留痕（NF-S03），**不含正文**。
- ``experiment`` / ``experiment_assignment``：A/B 实验定义与稳定分组。
- ``annotation_set`` / ``annotation``：金标集与标注明细（含 Cohen's κ）。
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

__all__ = [
    "Annotation",
    "AnnotationSet",
    "EgressLog",
    "Experiment",
    "ExperimentAssignment",
    "LlmCallAudit",
]


class LlmCallAudit(Base):
    """LLM 调用审计。

    ``provider`` 表示后端身份，``degraded`` 表示是否由降级链进入——
    两者语义不同（歧义 A-08），必须分开记录。
    """

    __tablename__ = "llm_call_audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    llm_call_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    content_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("content.id", ondelete="SET NULL"), nullable=True
    )
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    system_prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    user_prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    label_def_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    cost: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    pipeline_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default="now()"
    )

    __table_args__ = (
        CheckConstraint(
            "purpose IN ('tagging','arbiter','scoring','fewshot_mining')",
            name="ck_llm_audit_purpose",
        ),
        Index("ix_llm_audit_content", "content_id", "created_at"),
        Index("ix_llm_audit_purpose", "purpose", "created_at"),
        Index("ix_llm_audit_prompt", "prompt_hash"),
        Index("ix_llm_audit_created", "created_at"),
    )


class EgressLog(Base):
    """外发请求留痕。

    **不存正文**，只存目标、字节数与是否脱敏（NF-S03）。
    """

    __tablename__ = "egress_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    content_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("content.id", ondelete="SET NULL"), nullable=True
    )
    destination: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bytes_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pii_masked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    purpose: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default="now()"
    )

    __table_args__ = (
        Index("ix_egress_trace", "trace_id"),
        Index("ix_egress_created", "created_at"),
    )


class Experiment(Base):
    """A/B 实验定义。

    ``evidence_type`` 禁止 ``online_real``（架构 §5.7）——没有真实流量时，
    实验结论只能是离线模拟。
    """

    __tablename__ = "experiment"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    hypothesis: Mapped[str | None] = mapped_column(Text, nullable=True)
    baseline_algo: Mapped[str | None] = mapped_column(String(64), nullable=True)
    treatment_algo: Mapped[str | None] = mapped_column(String(64), nullable=True)
    traffic_split: Mapped[dict] = mapped_column(JSONBType, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    metric_definitions: Mapped[list] = mapped_column(JSONBType, nullable=False, default=list)
    evidence_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="offline_simulation"
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default="now()"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','running','paused','ended')", name="ck_experiment_status"
        ),
        CheckConstraint(
            "evidence_type IN ('offline_simulation','online_gray')",
            name="ck_experiment_evidence_type",
        ),
    )


class ExperimentAssignment(Base):
    """用户分组。

    分组用 ``hash(user_id, salt)`` 稳定计算，同一用户永远落在同一组。
    """

    __tablename__ = "experiment_assignment"

    user_id: Mapped[str] = mapped_column(String(64), nullable=False, primary_key=True)
    experiment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("experiment.id", ondelete="CASCADE"), primary_key=True
    )
    group: Mapped[str] = mapped_column("group", String(16), nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default="now()"
    )
    hash_salt: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        CheckConstraint("\"group\" IN ('control','treatment')", name="ck_assignment_group"),
        Index("ix_assignment_user", "user_id"),
    )


class AnnotationSet(Base):
    """金标集。

    ``synthetic_gold`` 必须显式标记（Q11）：合成金标与真实人工标注不可混算。
    """

    __tablename__ = "annotation_set"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=500)
    label_def_version: Mapped[str] = mapped_column(String(64), nullable=False)
    synthetic_gold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="synthetic")
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default="now()"
    )
    author: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Annotation(Base):
    """标注明细。"""

    __tablename__ = "annotation"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    set_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("annotation_set.id", ondelete="CASCADE"), nullable=False
    )
    content_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("content.id", ondelete="CASCADE"), nullable=False
    )
    labels: Mapped[list] = mapped_column(JSONBType, nullable=False)
    annotator: Mapped[str] = mapped_column(String(64), nullable=False)
    agreement: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    is_gold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default="now()"
    )

    __table_args__ = (
        UniqueConstraint("set_id", "content_id", "annotator", name="ux_annotation_unique"),
        CheckConstraint(
            "agreement IS NULL OR (agreement >= -1 AND agreement <= 1)",
            name="ck_annotation_agreement",
        ),
        Index("ix_annotation_set_gold", "set_id", "is_gold"),
        Index("ix_annotation_content", "content_id"),
    )
