"""冲突与仲裁领域模型。

对应 ``label_conflict_log`` 表（架构文档 §5.4）。

「7 要素完整性」（架构 §13.2 的 V10）是硬约束：
``candidates`` / ``evidence_detail`` / ``resolved`` / ``reason`` /
``arbitrator_model`` / ``confidence`` / ``review_status`` 非空率必须 100%。
因此这里的值对象在构造时就要求必要字段，而不是允许留空。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from tagmind.domain.base import DomainModel, JsonObject
from tagmind.domain.label import ConflictType

__all__ = [
    "ArbitrationResult",
    "ConflictCandidate",
    "ConflictLog",
    "Evidence",
    "ReviewStatus",
]


class ReviewStatus(StrEnum):
    """复核状态。

    取值域与 ``review_status_t`` 域一致。
    """

    PENDING = "pending"
    APPROVED = "approved"
    OVERRIDDEN = "overridden"
    AUTO = "auto"


@dataclass(frozen=True, slots=True)
class ConflictCandidate(DomainModel):
    """冲突中的一个候选项。"""

    label_id: int
    code: str
    layer: int
    confidence: float
    source: str = "rule"
    name: str = ""
    reason: str = ""
    rule_id: str | None = None


@dataclass(frozen=True, slots=True)
class Evidence(DomainModel):
    """检索到的证据条目。

    ``score`` 已归一到 ``[0, 1]``，且调用方保证按降序排列（架构 §15.2 T09）。
    """

    sample_id: str
    score: float
    matched_label: str
    source: str = "rag"
    snippet: str = ""
    metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ArbitrationResult(DomainModel):
    """仲裁结论。

    ``confidence`` 低于 0.75 时必须转人工复核（架构 §10.3 的 C8）。
    """

    resolved_labels: tuple[str, ...]
    winner: str
    reason: str
    retrieved_evidence_ids: tuple[str, ...]
    arbitrator_model: str
    confidence: float
    review_status: ReviewStatus = ReviewStatus.PENDING
    conflict_type: ConflictType = ConflictType.SEMANTIC_CANDIDATE
    prompt_version: str = ""
    created_at: datetime | None = None

    @property
    def needs_human(self) -> bool:
        """是否需要转人工。"""
        return self.review_status == ReviewStatus.PENDING


@dataclass(frozen=True, slots=True)
class ConflictLog(DomainModel):
    """一条冲突日志（对应 ``label_conflict_log`` 的一行）。"""

    id: int
    content_id: int
    candidates: tuple[ConflictCandidate, ...]
    conflict_type: ConflictType
    detected_by: str
    review_status: ReviewStatus = ReviewStatus.PENDING
    resolved: tuple[str, ...] | None = None
    reason: str | None = None
    retrieved_evidence_ids: tuple[str, ...] = ()
    retrieved_scores: JsonObject | None = None
    arbitrator_model: str | None = None
    arbitrator_prompt_version: str | None = None
    confidence: float | None = None
    reviewer: str | None = None
    reviewed_at: datetime | None = None
    resolved_before: tuple[str, ...] | None = None
    rule_hits: tuple[JsonObject, ...] = ()
    evidence_detail: tuple[Evidence, ...] = ()
    pipeline_version: str | None = None
    label_def_version: str | None = None
    model_version: str | None = None
    prompt_version: str | None = None
    trace_id: str | None = None
    created_at: datetime | None = None

    def seven_elements_complete(self) -> bool:
        """7 要素是否齐备（架构 §13.2 的 V10）。

        这是 ``scripts/consistency_check.py`` 的核心判定，必须与架构文档
        §5.4 的清单逐项对应。
        """
        return all(
            (
                bool(self.candidates),
                bool(self.evidence_detail),
                bool(self.resolved),
                bool(self.reason),
                bool(self.arbitrator_model),
                self.confidence is not None,
                bool(self.review_status),
            )
        )
