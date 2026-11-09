"""质量审核领域模型。

对应质量闸门双通道（规则通道 + LLM 通道）的产物。

关键语义（架构 §10.3 / PRD §5.4）：**规则一票否决不可被 LLM 高分翻案**；
双通道不可用时置 ``degraded=True`` 且转 ``needs_review``，**不默认放行**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from tagmind.domain.base import DomainModel, JsonObject
from tagmind.domain.content import QualityStatus

__all__ = [
    "LlmScore",
    "QualityVerdict",
    "RejectReason",
    "RuleHit",
]


class RejectReason(StrEnum):
    """拒绝原因分类。

    取值写入 ``content.reject_reason.code``，与 §5.2 的 JSONB 契约一致。
    """

    RULE_HIT = "rule_hit"
    LLM_LOW_SCORE = "llm_low_score"
    TOO_SHORT = "too_short"
    SENSITIVE_WORD = "sensitive_word"
    AD_RESIDUE = "ad_residue"
    INSUFFICIENT_INFO = "insufficient_info"
    VETO = "veto"


@dataclass(frozen=True, slots=True)
class RuleHit(DomainModel):
    """规则通道的一次命中。

    ``span`` 是命中位置 ``(start, end)``，用于前端高亮与人工复核定位。
    """

    rule_id: str
    category: str
    matched: str
    span: tuple[int, int] = (0, 0)
    detail: JsonObject = field(default_factory=dict)

    @property
    def is_veto(self) -> bool:
        """是否为一票否决类命中。

        一票否决的命中不能被 LLM 评分翻案（PRD P4-QA-003）。
        """
        return self.category in {"sensitive_word", "ad_residue", "veto"}


@dataclass(frozen=True, slots=True)
class LlmScore(DomainModel):
    """LLM 评分通道的输出。

    ``score`` 落在 ``[0, 10]``；``degraded=True`` 表示评分来自降级链路，
    此时结论必须降级为 ``needs_review``。
    """

    score: float
    reason: str
    model: str = ""
    degraded: bool = False
    parse_failed: bool = False


@dataclass(frozen=True, slots=True)
class QualityVerdict(DomainModel):
    """质量闸门结论。"""

    status: QualityStatus
    final_score: float
    rule_score: float | None = None
    llm_score: float | None = None
    hits: tuple[RuleHit, ...] = ()
    reject_reason: RejectReason | None = None
    reason: str = ""
    degraded: bool = False
    pipeline_version: str = ""
    created_at: datetime | None = None

    @property
    def has_veto(self) -> bool:
        """是否存在一票否决命中。"""
        return any(hit.is_veto for hit in self.hits)

    def to_payload(self) -> JsonObject:
        """转成 ``content.reject_reason`` JSONB 的结构。"""
        return {
            "code": str(self.reject_reason) if self.reject_reason else None,
            "detail": {
                "final_score": self.final_score,
                "rule_score": self.rule_score,
                "llm_score": self.llm_score,
                "degraded": self.degraded,
                "reason": self.reason,
            },
            "hits": [
                {
                    "rule_id": hit.rule_id,
                    "category": hit.category,
                    "span": list(hit.span),
                    "matched": hit.matched,
                }
                for hit in self.hits
            ],
        }
