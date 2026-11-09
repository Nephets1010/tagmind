"""全部 ORM 模型导出。

**必须显式导入所有模型模块**：SQLAlchemy 的 ``Base.metadata`` 只包含「已
定义」的表。若某个模型模块没被导入，`alembic` 的 ``target_metadata`` 就会漏掉
那张表，迁移脚本 autogenerate 会试图把它删掉。

因此本文件是 metadata 完整性的保证点，不是「方便导入」的糖。
"""

from __future__ import annotations

from tagmind.db.models.audit import (
    Annotation,
    AnnotationSet,
    EgressLog,
    Experiment,
    ExperimentAssignment,
    LlmCallAudit,
)
from tagmind.db.models.content import Content
from tagmind.db.models.interaction import ExposureLog, UserProfile
from tagmind.db.models.taxonomy import LabelDef
from tagmind.db.models.vector import ContentVector
from tagmind.db.models.workflow import (
    LabelConflictLog,
    LabelDriftCheck,
    ReviewRecord,
    ReviewTask,
)

#: 全部物理表名（16 张，歧义 A-03 的裁决）。
ALL_TABLES: tuple[str, ...] = (
    "content",
    "label_def",
    "label_conflict_log",
    "user_profile",
    "exposure_log",
    "content_vector",
    "llm_call_audit",
    "review_task",
    "review_record",
    "experiment",
    "experiment_assignment",
    "annotation_set",
    "annotation",
    "label_drift_check",
    "pii_mask_map",
    "egress_log",
)

__all__ = [
    "ALL_TABLES",
    "Annotation",
    "AnnotationSet",
    "Content",
    "ContentVector",
    "EgressLog",
    "Experiment",
    "ExperimentAssignment",
    "ExposureLog",
    "LabelConflictLog",
    "LabelDef",
    "LabelDriftCheck",
    "LlmCallAudit",
    "ReviewRecord",
    "ReviewTask",
    "UserProfile",
]
