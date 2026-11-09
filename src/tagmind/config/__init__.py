"""配置包：环境变量映射、常量、阈值与提示词模板。

- :mod:`tagmind.config.settings` 是 ``TAGMIND_*`` 环境变量的**唯一**映射点。
- :mod:`tagmind.config.constants` 放不可变枚举（层定义、配额、状态取值域）。
- :mod:`tagmind.config.thresholds` 集中所有可调阈值，便于灰度与审计。
- :mod:`tagmind.config.prompt_templates` 放提示词文本与其版本号。
"""

from __future__ import annotations

from tagmind.config.constants import (
    ALGO_VALUES,
    BEHAVIOR_ACTIONS,
    CONFLICT_TYPES,
    CONTENT_STATUSES,
    LAYER_NAMES,
    LAYER_QUOTAS,
    LLM_BACKENDS,
    QUALITY_STATUSES,
    RECALL_SOURCES,
    REVIEW_STATUSES,
    VALID_INTERACTIONS,
)
from tagmind.config.settings import Settings, get_settings

__all__ = [
    "ALGO_VALUES",
    "BEHAVIOR_ACTIONS",
    "CONFLICT_TYPES",
    "CONTENT_STATUSES",
    "LAYER_NAMES",
    "LAYER_QUOTAS",
    "LLM_BACKENDS",
    "QUALITY_STATUSES",
    "RECALL_SOURCES",
    "REVIEW_STATUSES",
    "VALID_INTERACTIONS",
    "Settings",
    "get_settings",
]
