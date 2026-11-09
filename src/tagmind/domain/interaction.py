"""交互领域模型：ExposureEvent / BehaviorAction / RequestContext。

对应 ``exposure_log`` 表（架构文档 §5.6）。

「有效交互」口径（歧义 A-09 的裁决）：``action ∈ {click, dwell}`` 计入交互数；
``skip`` 与 ``view`` 不计入交互数，但 ``view`` 会以 0.5 的权重计入
``behavior_count``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from tagmind.domain.base import DomainModel

__all__ = [
    "BehaviorAction",
    "ExposureEvent",
    "RequestContext",
    "effective_interaction_weight",
    "is_effective_interaction",
]


class BehaviorAction(StrEnum):
    """行为类型。

    取值域与 ``action_t`` 域一致。
    """

    VIEW = "view"
    CLICK = "click"
    SKIP = "skip"
    DWELL = "dwell"


#: 计入「有效交互」的动作。
EFFECTIVE_ACTIONS: frozenset[BehaviorAction] = frozenset(
    {BehaviorAction.CLICK, BehaviorAction.DWELL}
)

#: 各动作对 ``behavior_count`` 的贡献权重。
BEHAVIOR_COUNT_WEIGHTS: dict[BehaviorAction, float] = {
    BehaviorAction.VIEW: 0.5,
    BehaviorAction.CLICK: 1.0,
    BehaviorAction.SKIP: 0.0,
    BehaviorAction.DWELL: 1.0,
}


def is_effective_interaction(action: BehaviorAction) -> bool:
    """判断某动作是否算「有效交互」。"""
    return action in EFFECTIVE_ACTIONS


def effective_interaction_weight(action: BehaviorAction) -> float:
    """该动作对 ``behavior_count`` 的贡献。"""
    return BEHAVIOR_COUNT_WEIGHTS.get(action, 0.0)


@dataclass(frozen=True, slots=True)
class ExposureEvent(DomainModel):
    """一次曝光 / 行为事件。

    ``request_id`` 是跨表关联的锚点：
    ``exposure_log.request_id`` ↔ ``llm_call_audit.trace_id`` ↔
    ``label_conflict_log.trace_id``。
    """

    user_id: str
    content_id: int
    action: BehaviorAction
    ts: datetime
    request_id: str
    rank_pos: int | None = None
    score: float | None = None
    algo: str | None = None
    recall_source: str | None = None
    exp_group: str = "default"
    dwell_ms: int | None = None
    request_latency_ms: int | None = None
    synthetic: bool = False
    pipeline_version: str | None = None

    @property
    def is_effective(self) -> bool:
        """是否算有效交互。"""
        return is_effective_interaction(self.action)


@dataclass(frozen=True, slots=True)
class RequestContext(DomainModel):
    """一次推荐 / 打标请求的上下文。

    ``cache_bypass=True`` 是离线回放的**硬性要求**（ADR-0006）：
    回放必须按回放时间窗从 ``exposure_log`` 重算热门榜，不能读线上缓存，
    否则仿真结论不可复现。
    """

    request_id: str
    trace_id: str
    user_id: str | None = None
    content_id: int | None = None
    replay_ts: datetime | None = None
    cache_bypass: bool = False
    debug_level: int = 0
    exp_group: str = "default"

    @property
    def is_replay(self) -> bool:
        """是否处于回放模式。"""
        return self.replay_ts is not None
