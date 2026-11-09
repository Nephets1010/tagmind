"""向量领域模型：DenseVector / TagVector / SimilarityHit。

对应 ``content_vector`` 表（架构文档 §5.7）。

**关键语义**（歧义 A-05 的裁决）：

- ``content_vector.tag_vector``（JSONB）是标签向量的**真相源**，存未归一化的
  ``v(c)``，即 ``w_layer × idf × confidence``。
- 在线读路径走进程内 CSR 稀疏矩阵（阶段 3 实现），本阶段只建表与仓储。

``source='derived'`` 的祖先标签**不参与**向量计算（架构 §11.4）——
否则会人为抬高祖先层权重。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from tagmind.domain.base import DomainModel, JsonObject

__all__ = [
    "DenseVector",
    "SimilarityHit",
    "TagVector",
]


@dataclass(frozen=True, slots=True)
class DenseVector(DomainModel):
    """稠密向量（BGE-M3 等）。

    ``embedding`` 存未归一化的向量，``norm`` 存 L2 模长。
    """

    content_id: int
    embedding: tuple[float, ...] = ()
    embedding_model: str = ""
    embedding_version: str = ""
    dim: int = 0
    norm: float = 1.0

    def validate_dim(self) -> bool:
        """实际维度是否与声明的 ``dim`` 一致。"""
        return self.dim == len(self.embedding)

    def normalized(self) -> tuple[float, ...]:
        """返回 L2 归一化后的向量。"""
        if self.norm <= 0.0:
            return self.embedding
        return tuple(value / self.norm for value in self.embedding)


@dataclass(frozen=True, slots=True)
class TagVector(DomainModel):
    """标签稀疏向量。

    ``values`` 是 ``{label_id: 加权值}``，其中加权值 =
    ``w_layer × idf × confidence``；``norm`` 是 L2 模长。
    """

    content_id: int
    values: dict[int, float] = field(default_factory=dict)
    norm: float = 0.0
    tag_space_version: str = ""
    metadata: JsonObject = field(default_factory=dict)

    def compute_norm(self) -> float:
        """按当前 ``values`` 计算 L2 模长。"""
        return math.sqrt(sum(value * value for value in self.values.values()))

    def is_norm_consistent(self, tolerance: float = 1e-6) -> bool:
        """存储的 ``norm`` 是否与实际值一致（架构 §13.2 的 V3）。"""
        return abs(self.norm - self.compute_norm()) < tolerance

    def normalized(self) -> dict[int, float]:
        """返回归一化后的稀疏向量。"""
        if self.norm <= 0.0:
            return dict(self.values)
        return {key: value / self.norm for key, value in self.values.items()}

    def to_jsonb(self) -> JsonObject:
        """转成 ``content_vector.tag_vector`` 的 JSONB 形态。"""
        return {str(key): value for key, value in self.values.items()}

    @classmethod
    def from_jsonb(
        cls, content_id: int, payload: JsonObject, *, tag_space_version: str = ""
    ) -> TagVector:
        """从 JSONB 还原标签向量。

        Args:
            content_id: 内容 ID。
            payload: JSONB 内容，键为 label_id 字符串。
            tag_space_version: 特征空间版本。

        Returns:
            还原出的 :class:`TagVector`；``norm`` 由实际值计算。
        """
        values: dict[int, float] = {}
        for key, value in payload.items():
            try:
                label_id = int(key)
            except (TypeError, ValueError):
                continue
            if isinstance(value, int | float):
                values[label_id] = float(value)
        return cls(
            content_id=content_id,
            values=values,
            norm=math.sqrt(sum(value * value for value in values.values())),
            tag_space_version=tag_space_version,
        )


@dataclass(frozen=True, slots=True)
class SimilarityHit(DomainModel):
    """一次相似度检索的命中。

    ``score`` 统一为「越大越相似」的余弦值——两个向量后端的距离方向差异
    在适配层抹平，调用方无需感知（架构 §14.6）。
    """

    content_id: int
    score: float
    recall_source: str = ""
    label_ids: tuple[int, ...] = ()
    metadata: JsonObject = field(default_factory=dict)
