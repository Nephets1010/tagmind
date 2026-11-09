"""用户画像领域模型。

对应 ``user_profile`` 表（架构文档 §5.5）。

**双字段方案**（歧义 A-02 的裁决）：

- ``tag_weights``：归一化后的**非负**权重，和为 1，用于召回打分与验收断言。
- ``label_affinity``：带符号的**原始分**，可负，用于表达「被 skip 的标签权重 ≤ 0」
  以及漂移解释。

两个字段并存，才让 PRD P4-USR-001 的两条断言同时成立而不互相矛盾。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from tagmind.domain.base import DomainModel, JsonObject

__all__ = [
    "ProfileState",
    "TagWeights",
    "UserProfile",
]


class ProfileState(StrEnum):
    """画像状态。"""

    COLD_START = "cold_start"
    WARM = "warm"
    DRIFTED = "drifted"


@dataclass(frozen=True, slots=True)
class TagWeights(DomainModel):
    """标签权重集合。

    Attributes:
        weights: 归一化后的非负权重，键为 label_id，和为 1。
        affinity: 带符号原始分，键为 label_id，可为负。
    """

    weights: dict[int, float] = field(default_factory=dict)
    affinity: dict[int, float] = field(default_factory=dict)

    def weight_sum(self) -> float:
        """权重之和（归一化后应为 1）。"""
        return float(sum(self.weights.values()))

    def is_normalized(self, tolerance: float = 1e-6) -> bool:
        """是否已归一化（误差 ≤ ``tolerance``）。"""
        return abs(self.weight_sum() - 1.0) <= tolerance

    def is_non_negative(self) -> bool:
        """权重是否全部非负。"""
        return all(value >= 0.0 for value in self.weights.values())

    def dims(self) -> int:
        """非零权重维度数。"""
        return len(self.weights)

    def l2_norm(self) -> float:
        """L2 模长，供余弦相似度使用。"""
        total = 0.0
        for value in self.weights.values():
            squared = float(value) * float(value)
            total += squared
        return float(total**0.5)

    def top_labels(self, limit: int = 10) -> tuple[tuple[int, float], ...]:
        """取权重最高的若干标签。

        排序用「权重降序 + label_id 升序」作为确定性 tie-breaker，
        保证同样输入永远得到同样输出（Q3 的可复现性要求）。
        """
        return tuple(sorted(self.weights.items(), key=lambda kv: (-kv[1], kv[0]))[:limit])

    def to_jsonb(self) -> JsonObject:
        """转成 JSONB 形态：label_id 的键转成字符串。"""
        return {
            "weights": {str(k): v for k, v in self.weights.items()},
            "affinity": {str(k): v for k, v in self.affinity.items()},
        }


@dataclass(frozen=True, slots=True)
class UserProfile(DomainModel):
    """用户画像。"""

    user_id: str
    tag_weights: TagWeights = field(default_factory=TagWeights)
    cold_start: bool = True
    confidence: float = 0.0
    behavior_count: int = 0
    label_dims: int = 0
    decay_lambda: float = 0.95
    last_behavior_at: datetime | None = None
    drift_detected: bool = False
    drift_score: float | None = None
    model_version: str | None = None
    tag_space_version: str | None = None
    label_weight_norm: float = 0.0
    cold_start_threshold: int = 10
    weights_sum: float = 0.0
    synthetic: bool = False
    updated_at: datetime | None = None

    @property
    def state(self) -> ProfileState:
        """当前画像状态。

        判定顺序：漂移优先，其次冷启动，最后正常。
        """
        if self.drift_detected:
            return ProfileState.DRIFTED
        if self.cold_start or self.behavior_count < self.cold_start_threshold:
            return ProfileState.COLD_START
        return ProfileState.WARM

    def is_consistent(self, tolerance: float = 1e-6) -> bool:
        """画像自洽性校验（架构 §13.2 的 V6/V8/V9）。

        校验三件事：

        1. 归一化：``|Σ tag_weights - 1| ≤ tolerance``（非冷启动用户）。
        2. 冷启动一致性：``cold_start == (confidence == 0)``。
        3. 维度一致：``label_dims == len(tag_weights)``。
        """
        if self.cold_start:
            if self.confidence != 0.0:
                return False
        elif not self.tag_weights.is_normalized(tolerance):
            return False
        return self.label_dims == self.tag_weights.dims()

    def to_jsonb_weights(self) -> JsonObject:
        """把权重转成 ``user_profile.tag_weights`` 的 JSONB 形态。"""
        return {str(k): v for k, v in self.tag_weights.weights.items()}

    def to_jsonb_affinity(self) -> JsonObject:
        """把带符号原始分转成 ``user_profile.label_affinity`` 的 JSONB 形态。"""
        return {str(k): v for k, v in self.tag_weights.affinity.items()}
