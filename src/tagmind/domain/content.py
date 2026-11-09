"""内容领域模型：Content / CleanStats / Chunk / 状态枚举。

对应 ``content`` 表（架构文档 §5.2）。这里只放**领域语义**，
ORM 映射在 :mod:`tagmind.db.models.content`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from tagmind.domain.base import DomainModel, JsonObject

__all__ = [
    "Chunk",
    "CleanStats",
    "Content",
    "ContentStatus",
    "QualityStatus",
]


class ContentStatus(StrEnum):
    """内容生命周期状态。

    取值域与 ``content_status_t`` 域（架构 §5.1）严格一致。
    """

    PENDING = "pending"
    PROCESSING = "processing"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"
    ARCHIVED = "archived"


class QualityStatus(StrEnum):
    """质量闸门结论。

    取值域与 ``quality_status_t`` 域一致。
    """

    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True, slots=True)
class CleanStats(DomainModel):
    """清洗统计。

    字段与 ``content.clean_stats`` JSONB 的契约一致（架构 §5.2）。
    各字段之和必须自洽：``clean_len + truncated_len`` 与 ``raw_len`` 的关系
    由清洗器保证，误差必须为 0（P4-PRE-004）。
    """

    raw_len: int = 0
    clean_len: int = 0
    truncated_len: int = 0
    removed_tags: int = 0
    removed_ads: int = 0
    removed_emoji: int = 0
    removed_noise: int = 0
    chunks: int = 0
    noise_chars: int = 0

    @property
    def removed_total(self) -> int:
        """被移除的字符总数。"""
        return self.removed_tags + self.removed_ads + self.removed_emoji + self.removed_noise

    @property
    def is_self_consistent(self) -> bool:
        """统计是否自洽。

        判定：``raw_len - removed_total - truncated_len == clean_len``。
        误差必须精确为 0，不允许四舍五入。
        """
        return self.raw_len - self.removed_total - self.truncated_len == self.clean_len


@dataclass(frozen=True, slots=True)
class Chunk(DomainModel):
    """内容分块。

    对应 ``content.chunks`` JSONB 的元素；``token_est`` 是估算值而非真实
    token 数——估算足够做预算控制，真实 token 数要等 LLM 侧才知道。
    """

    idx: int
    offset: int = 0
    length: int = 0
    token_est: int = 0


@dataclass(frozen=True, slots=True)
class Content(DomainModel):
    """内容实体。

    ``labels`` 保存最终通过 C1–C10 校验的标签；``label_raw`` 保存被过滤掉的
    低置信候选（架构 §5.2 的 JSONB 契约）。
    """

    id: int
    raw_text: str = ""
    clean_text: str = ""
    labels: tuple[JsonObject, ...] = ()
    label_raw: JsonObject | None = None
    label_confidence: JsonObject | None = None
    label_stats: JsonObject | None = None
    labels_version: str = ""
    quality_score: float | None = None
    quality_reason: str | None = None
    quality_status: QualityStatus = QualityStatus.NEEDS_REVIEW
    status: ContentStatus = ContentStatus.PENDING
    reject_reason: JsonObject | None = None
    clean_stats: CleanStats | None = None
    chunks: tuple[Chunk, ...] = ()
    pii_masked: bool = False
    pii_fields: tuple[str, ...] = ()
    source: str | None = None
    content_hash: str | None = None
    idempotency_key: str | None = None
    embedding_version: str | None = None
    model_version: str | None = None
    prompt_version: str | None = None
    pipeline_version: str = ""
    label_ids: tuple[int, ...] = ()
    layer_label_ids: tuple[int, ...] = ()
    l1_code: str | None = None
    l2_code: str | None = None
    synthetic: bool = False
    media_refs: tuple[JsonObject, ...] = ()
    created_at: datetime | None = None
    updated_at: datetime | None = None
    published_at: datetime | None = None

    @property
    def is_dispatchable(self) -> bool:
        """是否可进入分发索引。

        对应 ``ix_content_dispatchable`` 部分索引的谓词：
        ``status='approved' AND quality_status='approved'``。
        """
        return (
            self.status == ContentStatus.APPROVED
            and self.quality_status == QualityStatus.APPROVED
            and self.published_at is not None
        )

    @property
    def is_synthetic(self) -> bool:
        """是否为合成数据。

        Q10 要求合成数据必须显式标记，避免仿真结论被当成线上数据。
        """
        return self.synthetic

    def label_id_set(self) -> frozenset[int]:
        """从 ``labels`` 中抽出全部 label_id 集合。"""
        ids: set[int] = set()
        for item in self.labels:
            raw_id = item.get("label_id")
            if isinstance(raw_id, int):
                ids.add(raw_id)
        return frozenset(ids)


@dataclass(frozen=True, slots=True)
class ContentSummary(DomainModel):
    """内容摘要（列表页用，避免携带正文）。"""

    id: int
    status: ContentStatus
    quality_status: QualityStatus
    labels_version: str
    pipeline_version: str
    created_at: datetime | None = None
    label_count: int = 0
    synthetic: bool = False
    extra: JsonObject = field(default_factory=dict)
