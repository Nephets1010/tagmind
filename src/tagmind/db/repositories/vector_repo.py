"""向量仓储：向量 upsert / search / delete、HNSW 重建、标签倒排。

**与内容表同事务**（架构 §14.5）：``upsert_many`` 与 ``content`` 状态更新
在同一个事务里完成——向量与内容状态不一致会让召回结果指向未发布内容。

标签倒排的真相源是 ``content_vector.tag_vector``（JSONB，歧义 A-05）；
在线读路径的 CSR 稀疏矩阵由阶段 3 的 :mod:`tagmind.vector.tag_space` 构建，
本仓储只负责持久化与检索。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from tagmind.db.models.vector import ContentVector as ContentVectorModel
from tagmind.db.repositories.base import BaseRepository, FilterSpec
from tagmind.domain.vector import DenseVector, SimilarityHit, TagVector

__all__ = ["VectorRepository"]


class VectorRepository(BaseRepository[DenseVector]):
    """向量仓储。"""

    model = ContentVectorModel

    def __init__(self, session: Session) -> None:
        """初始化仓储。"""
        super().__init__(session)

    # --- 映射 ---------------------------------------------------------

    def _map_row_to_domain(self, row: Any) -> DenseVector:
        """ORM 行 → 领域对象。"""
        embedding = row.embedding
        if not isinstance(embedding, tuple | list):
            embedding = ()
        return DenseVector(
            content_id=row.content_id,
            embedding=tuple(float(value) for value in embedding),
            embedding_model=row.embedding_model,
            embedding_version=row.embedding_version,
            dim=row.dim,
            norm=float(row.norm),
        )

    def _map_domain_to_values(self, entity: DenseVector) -> dict[str, Any]:
        """领域对象 → 列值字典。"""
        return {
            "content_id": entity.content_id,
            "embedding": list(entity.embedding),
            "embedding_model": entity.embedding_model,
            "embedding_version": entity.embedding_version,
            "dim": entity.dim,
            "norm": entity.norm,
        }

    # --- 写入 ---------------------------------------------------------

    def upsert_many(self, records: list[TagVector]) -> int:
        """批量写入标签向量（与稠密向量共用一行）。

        使用 ``INSERT ... ON CONFLICT (content_id) DO UPDATE``，
        保证「一个内容一行向量」。

        Args:
            records: 标签向量列表。

        Returns:
            影响的行数。
        """
        if not records:
            return 0
        payload = []
        for record in records:
            metadata = record.metadata
            embedding = metadata.get("embedding", [])
            dim_value = metadata.get("dim", 1024)
            norm_value = metadata.get("norm", 1)
            payload.append(
                {
                    "content_id": record.content_id,
                    "embedding": list(embedding) if isinstance(embedding, list | tuple) else [],
                    "embedding_model": str(metadata.get("embedding_model", "")),
                    "embedding_version": str(metadata.get("embedding_version", "")),
                    "dim": int(dim_value) if isinstance(dim_value, int | float) else 1024,
                    "norm": float(norm_value) if isinstance(norm_value, int | float) else 1.0,
                    "tag_vector": record.to_jsonb(),
                    "tag_vector_norm": record.compute_norm(),
                    "tag_space_version": record.tag_space_version,
                }
            )
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(ContentVectorModel).values(payload)
        stmt = stmt.on_conflict_do_update(
            index_elements=["content_id"],
            set_={
                "embedding": stmt.excluded.embedding,
                "embedding_model": stmt.excluded.embedding_model,
                "embedding_version": stmt.excluded.embedding_version,
                "dim": stmt.excluded.dim,
                "norm": stmt.excluded.norm,
                "tag_vector": stmt.excluded.tag_vector,
                "tag_vector_norm": stmt.excluded.tag_vector_norm,
                "tag_space_version": stmt.excluded.tag_space_version,
            },
        )
        result = cast("CursorResult[Any]", self._session.execute(stmt))
        self._session.flush()
        return int(result.rowcount or 0)

    def get_tag_vector(self, content_id: int) -> TagVector | None:
        """取某内容的标签向量（JSONB 真相源）。"""
        stmt = select(ContentVectorModel).where(ContentVectorModel.content_id == content_id)
        row = self._session.execute(stmt).scalars().first()
        if row is None:
            return None
        return TagVector.from_jsonb(
            row.content_id, row.tag_vector or {}, tag_space_version=row.tag_space_version or ""
        )

    # --- 检索 ---------------------------------------------------------

    def search_by_tags(
        self,
        label_ids: list[int],
        *,
        top_k: int = 100,
        tag_space_version: str | None = None,
    ) -> list[SimilarityHit]:
        """按标签倒排检索。

        这里做的是**候选筛选**（JSONB GIN 索引），精确打分在进程内由 CSR
        稀疏矩阵完成（阶段 3）。

        Args:
            label_ids: 标签 ID 列表。
            top_k: 返回条数。
            tag_space_version: 特征空间版本过滤。

        Returns:
            命中列表。
        """
        if not label_ids:
            return []
        keys = [str(label_id) for label_id in label_ids]
        stmt = (
            select(ContentVectorModel.content_id)
            .where(ContentVectorModel.tag_vector.has_any(keys))
            .limit(top_k)
        )
        if tag_space_version:
            stmt = stmt.where(ContentVectorModel.tag_space_version == tag_space_version)
        rows: Sequence[int] = self._session.execute(stmt).scalars().all()
        return [
            SimilarityHit(content_id=int(row), score=0.0, recall_source="tag_preference")
            for row in rows
        ]

    def delete_many(self, content_ids: list[int]) -> int:
        """批量删除向量。

        Args:
            content_ids: 内容 ID 列表。

        Returns:
            删除的行数。
        """
        if not content_ids:
            return 0
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                delete(ContentVectorModel).where(ContentVectorModel.content_id.in_(content_ids))
            ),
        )
        self._session.flush()
        return int(result.rowcount or 0)

    def count(self, filters: FilterSpec | None = None) -> int:
        """统计向量行数（V1 校验：已发布内容的向量覆盖率）。"""
        stmt = select(func.count()).select_from(ContentVectorModel)
        if filters:
            for column, value in filters.conditions.items():
                stmt = stmt.where(getattr(ContentVectorModel, column) == value)
        return int(self._session.execute(stmt).scalar_one())

    def rebuild_index(self) -> None:
        """重建 HNSW 索引。

        PostgreSQL 的 HNSW 索引需要 ``REINDEX`` 才能在批量写入后恢复查询性能。
        """
        self._session.execute(text("REINDEX INDEX IF EXISTS ix_content_vector_hnsw"))
        self._session.flush()

    def orphan_count(self) -> int:
        """统计孤儿向量（对应内容不存在的向量行）。

        架构 §13.2 的 V4 要求该值为 0。
        """
        stmt = text(
            """
            SELECT COUNT(*)
            FROM content_vector v
            LEFT JOIN content c ON c.id = v.content_id
            WHERE c.id IS NULL
            """
        )
        return int(self._session.execute(stmt).scalar_one())

    def distinct_tag_space_versions(self) -> list[str]:
        """列出全部特征空间版本（V5 校验用）。"""
        stmt = select(ContentVectorModel.tag_space_version).distinct()
        return [str(row) for row in self._session.execute(stmt).scalars() if row]
