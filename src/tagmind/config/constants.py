"""常量表：层定义、配额、状态枚举、错误码、规则类别字典。

这里放的是**取值域**——它们在 DDL、API schema、提示词、校验器里都要引用，
集中在一处才能保证「DDL 的 CHECK 约束」与「代码里的枚举」不会漂移。

修改本文件前请先确认：对应的 PostgreSQL DOMAIN 是否也需要同步（架构 §5.1）。
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "ALGO_VALUES",
    "BEHAVIOR_ACTIONS",
    "CONFLICT_TYPES",
    "CONTENT_STATUSES",
    "ERROR_CODES",
    "EXCLUSIVE_GROUP_SENTIMENT",
    "LABEL_DEF_VERSIONS",
    "LAYER_DEFINITIONS",
    "LAYER_NAMES",
    "LAYER_QUOTAS",
    "LLM_BACKENDS",
    "MAX_LABEL_TOTAL",
    "MAX_SELECT_LIMIT",
    "MIN_LABEL_TOTAL",
    "QUALITY_STATUSES",
    "RECALL_SOURCES",
    "REVIEW_STATUSES",
    "VALID_INTERACTIONS",
]

#: 标签体系总层数（固定 12 层，不可无限下钻）。
LAYER_COUNT: Final[int] = 12

#: 严格树层（L1–L4）：一条内容每层只有一个主归属。
STRICT_TREE_LAYERS: Final[tuple[int, ...]] = (1, 2, 3, 4)

#: 开放多选层（L5–L12）：承载实体、属性、意图、情感等可并存维度。
OPEN_SET_LAYERS: Final[tuple[int, ...]] = (5, 6, 7, 8, 9, 10, 11, 12)

#: 层号 → 层名。
LAYER_NAMES: Final[dict[int, str]] = {
    1: "领域域",
    2: "一级类目",
    3: "二级类目",
    4: "主题簇",
    5: "事件与场景",
    6: "人物实体",
    7: "机构与品牌",
    8: "地理与场所",
    9: "商品与作品",
    10: "属性与量化",
    11: "消费意图",
    12: "情感立场风险",
}

#: 层号 → 每层配额（PRD §4.1 层级定位表）。
LAYER_QUOTAS: Final[dict[int, int]] = {
    1: 1,
    2: 1,
    3: 3,
    4: 3,
    5: 5,
    6: 5,
    7: 5,
    8: 5,
    9: 5,
    10: 8,
    11: 3,
    12: 3,
}

#: 层号 → 层语义职责。
LAYER_DEFINITIONS: Final[dict[int, str]] = {
    1: "内容所属的顶层事业域",
    2: "领域下的主业务类目",
    3: "类目下的细分业务方向",
    4: "具体议题 / 概念 / 事件簇",
    5: "内容描述的事件、情境、使用场景",
    6: "提及的公众人物 / 角色 / 职业群体",
    7: "涉及的公司、机构、品牌、IP",
    8: "发生的国家 / 省市 / 场馆 / 景区",
    9: "讲到的具体产品、型号、书影音、菜品",
    10: "时间、数值、状态、参数、颜色等客观属性",
    11: "内容唤起的受众潜在诉求",
    12: "主观情绪、立场与风险倾向",
}

#: 层号 → 是否进入召回特征（PRD §4.1：L10 不进召回特征）。
LAYER_IS_FEATURE: Final[dict[int, bool]] = {
    **dict.fromkeys(range(1, 13), True),
    10: False,
}

#: 标签体系总量下界（Q2 拍板值 1200–1800）。
MIN_LABEL_TOTAL: Final[int] = 1200

#: 标签体系总量上界（Q2 拍板值）。
MAX_LABEL_TOTAL: Final[int] = 1800

#: 单个标签的 ``max_select`` 上限（DDL ``ck_label_def_max_select``）。
MAX_SELECT_LIMIT: Final[int] = 8

#: 内容状态取值域。
CONTENT_STATUSES: Final[tuple[str, ...]] = (
    "pending",
    "processing",
    "approved",
    "rejected",
    "needs_review",
    "archived",
)

#: 质量状态取值域。
QUALITY_STATUSES: Final[tuple[str, ...]] = ("approved", "rejected", "needs_review")

#: 冲突类型取值域。
CONFLICT_TYPES: Final[tuple[str, ...]] = (
    "parent",
    "mutual",
    "exclusive",
    "semantic_candidate",
)

#: 复核状态取值域。
REVIEW_STATUSES: Final[tuple[str, ...]] = ("pending", "approved", "overridden", "auto")

#: 行为类型取值域。
BEHAVIOR_ACTIONS: Final[tuple[str, ...]] = ("view", "click", "skip", "dwell")

#: 有效交互口径（歧义 A-09 裁决）：``view`` / ``skip`` 不计入交互数。
VALID_INTERACTIONS: Final[tuple[str, ...]] = ("click", "dwell")

#: 推荐算法取值域（歧义 A-06 裁决）。
#:
#: ``algo`` 必须反映**实际使用的后端**——LightFM 不可用时只能是
#: ``fallback_cf`` 或 ``recall_only``，绝不标成 ``lightfm``（ADR-0005）。
ALGO_VALUES: Final[tuple[str, ...]] = (
    "lightfm",
    "fallback_cf",
    "recall_only",
    "hot",
    "latest",
    "explore",
)

#: 召回来源取值域。
RECALL_SOURCES: Final[tuple[str, ...]] = (
    "tag_preference",
    "vector_similarity",
    "collaborative",
    "hot",
    "latest",
    "explore",
)

#: LLM 后端档位（ADR-0003）：``deterministic`` 是一等公民，不是占位符。
LLM_BACKENDS: Final[tuple[str, ...]] = ("deterministic", "llm_local", "llm_api")

#: LLM 供应商身份（歧义 A-08：``provider`` 是后端身份，与 ``degraded`` 不同）。
LLM_PROVIDERS: Final[tuple[str, ...]] = ("deterministic", "ollama", "zhipu")

#: 内置互斥组名。
EXCLUSIVE_GROUP_SENTIMENT: Final[str] = "sentiment_polarity"

#: 标签体系版本号（发布时递增；灰度期会有两个版本并存）。
LABEL_DEF_VERSIONS: Final[tuple[str, ...]] = ("ld_v1",)

#: 错误码 → 异常类名的映射（架构 §14.3）。用于一致性自检与文档生成。
ERROR_CODES: Final[dict[str, str]] = {
    "TagmindError": "TM_0000",
    "ValidationError": "TM_1000",
    "NotFoundError": "TM_1001",
    "IdempotencyConflict": "TM_1002",
    "OptimisticLockError": "TM_1003",
    "AuthError": "TM_2000",
    "PermissionDenied": "TM_2001",
    "RateLimitError": "TM_2002",
    "DependencyUnavailable": "TM_3000",
    "LLMBackendError": "TM_3001",
    "LLMFatalError": "TM_3002",
    "VectorStoreError": "TM_3003",
    "DatabaseError": "TM_3004",
    "ConstraintViolation": "TM_4000",
    "ConflictArbitrationError": "TM_4001",
    "QualityRejected": "TM_4002",
    "ReviewRequired": "TM_4003",
    "ConfigError": "TM_5000",
    "BackendUnavailable": "TM_5001",
    "InternalError": "TM_5002",
}

#: 规则类别字典：规则 ID 前缀 → 类别名。质量通道按类别做差异化阈值。
RULE_CATEGORIES: Final[dict[str, str]] = {
    "SW": "sensitive_word",
    "AD": "ad_residue",
    "MK": "marketing",
    "LN": "length",
    "RP": "repetition",
    "PT": "punctuation",
}
