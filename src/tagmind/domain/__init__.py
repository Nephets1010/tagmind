"""领域层：纯业务模型。

**硬性约束（架构文档 §14.2「铁律」）**：本子包**不得**导入 FastAPI、
SQLAlchemy、Pydantic、NumPy、Celery、Redis、psycopg 等任何框架或基础设施依赖。
只允许 Python 标准库（``dataclasses`` / ``enum`` / ``datetime`` / ``typing``）。

这样做的目的：领域规则（12 层标签硬约束 C1–C10、权重计算、召回打分）可以被
纯单元测试快速覆盖，不需要启动数据库、HTTP 服务或消息队列。

约束由 ``tests/unit/test_domain_purity.py`` 的 AST 扫描强制执行——靠约定守不住，
必须靠 CI 拦。
"""

from __future__ import annotations

from tagmind.domain.base import DomainModel, JsonObject, to_jsonable
from tagmind.domain.conflict import (
    ArbitrationResult,
    ConflictCandidate,
    Evidence,
    ReviewStatus,
)
from tagmind.domain.content import (
    Chunk,
    CleanStats,
    Content,
    ContentStatus,
    QualityStatus,
)
from tagmind.domain.interaction import (
    BehaviorAction,
    ExposureEvent,
    RequestContext,
)
from tagmind.domain.label import (
    ConflictType,
    LabelCandidate,
    LabelDef,
    LabelNode,
    LabelSet,
    LabelSource,
)
from tagmind.domain.profile import ProfileState, TagWeights, UserProfile
from tagmind.domain.quality import LlmScore, QualityVerdict, RejectReason, RuleHit
from tagmind.domain.vector import DenseVector, SimilarityHit, TagVector
from tagmind.domain.versioning import (
    EvidenceType,
    LabelDefVersion,
    MetricValue,
    PipelineVersion,
    PromptVersion,
)

__all__ = [
    "ArbitrationResult",
    "BehaviorAction",
    "Chunk",
    "CleanStats",
    "ConflictCandidate",
    "ConflictType",
    "Content",
    "ContentStatus",
    "DenseVector",
    "DomainModel",
    "Evidence",
    "EvidenceType",
    "ExposureEvent",
    "JsonObject",
    "LabelCandidate",
    "LabelDef",
    "LabelDefVersion",
    "LabelNode",
    "LabelSet",
    "LabelSource",
    "LlmScore",
    "MetricValue",
    "PipelineVersion",
    "ProfileState",
    "PromptVersion",
    "QualityStatus",
    "QualityVerdict",
    "RejectReason",
    "RequestContext",
    "ReviewStatus",
    "RuleHit",
    "SimilarityHit",
    "TagVector",
    "TagWeights",
    "UserProfile",
    "to_jsonable",
]
