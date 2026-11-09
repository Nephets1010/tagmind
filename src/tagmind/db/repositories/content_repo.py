"""内容仓储：内容 CRUD、幂等键查询、标签/状态更新、标签过滤。

事务语义（架构 §14.5）：``upsert_labels`` 必须在**单事务**内写
``labels`` + ``label_raw`` + ``label_stats`` + ``labels_version`` +
``pipeline_version``——这五个字段必须同生共死，否则会出现「标签是新的、
版本号是旧的」这种无法解释的脏数据。
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy import BigInteger, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from tagmind.db.models.content import Content as ContentModel
from tagmind.db.repositories.base import BaseRepository, FilterSpec
from tagmind.domain.content import (
    Chunk,
    CleanStats,
    Content,
    ContentStatus,
    QualityStatus,
)

__all__ = ["ContentRepository"]


class ContentRepository(BaseRepository[Content]):
    """内容仓储。"""

    model = ContentModel

    def __init__(self, session: Session) -> None:
        """初始化仓储。"""
        super().__init__(session)

    # --- 映射 ---------------------------------------------------------

    def _map_row_to_domain(self, row: Any) -> Content:
        """ORM 行 → 领域对象。"""
        clean_stats = row.clean_stats
        return Content(
            id=row.id,
            raw_text=row.raw_text,
            clean_text=row.clean_text,
            labels=tuple(row.labels or ()),
            label_raw=row.label_raw,
            label_confidence=row.label_confidence,
            label_stats=row.label_stats,
            labels_version=row.labels_version,
            quality_score=float(row.quality_score) if row.quality_score is not None else None,
            quality_reason=row.quality_reason,
            quality_status=QualityStatus(row.quality_status),
            status=ContentStatus(row.status),
            reject_reason=row.reject_reason,
            clean_stats=(CleanStats(**clean_stats) if isinstance(clean_stats, dict) else None),
            chunks=tuple(Chunk(**chunk) for chunk in (row.chunks or ()) if isinstance(chunk, dict)),
            pii_masked=row.pii_masked,
            pii_fields=tuple(row.pii_fields or ()),
            source=row.source,
            content_hash=row.content_hash,
            idempotency_key=row.idempotency_key,
            embedding_version=row.embedding_version,
            model_version=row.model_version,
            prompt_version=row.prompt_version,
            pipeline_version=row.pipeline_version,
            label_ids=tuple(row.label_ids or ()),
            layer_label_ids=tuple(row.layer_label_ids or ()),
            l1_code=row.l1_code,
            l2_code=row.l2_code,
            synthetic=row.synthetic,
            media_refs=tuple(row.media_refs or ()),
            created_at=row.created_at,
            updated_at=row.updated_at,
            published_at=row.published_at,
        )

    def _map_domain_to_values(self, entity: Content) -> dict[str, Any]:
        """领域对象 → 列值字典。"""
        return {
            "id": entity.id,
            "raw_text": entity.raw_text,
            "clean_text": entity.clean_text,
            "labels": list(entity.labels),
            "label_raw": entity.label_raw,
            "label_confidence": entity.label_confidence,
            "label_stats": entity.label_stats,
            "labels_version": entity.labels_version,
            "quality_score": entity.quality_score,
            "quality_reason": entity.quality_reason,
            "quality_status": str(entity.quality_status),
            "status": str(entity.status),
            "reject_reason": entity.reject_reason,
            "clean_stats": entity.clean_stats.to_dict() if entity.clean_stats else None,
            "chunks": [chunk.to_dict() for chunk in entity.chunks] or None,
            "pii_masked": entity.pii_masked,
            "pii_fields": list(entity.pii_fields) or None,
            "source": entity.source,
            "content_hash": entity.content_hash,
            "idempotency_key": entity.idempotency_key,
            "embedding_version": entity.embedding_version,
            "model_version": entity.model_version,
            "prompt_version": entity.prompt_version,
            "pipeline_version": entity.pipeline_version,
            "label_ids": list(entity.label_ids),
            "layer_label_ids": list(entity.layer_label_ids),
            "l1_code": entity.l1_code,
            "l2_code": entity.l2_code,
            "synthetic": entity.synthetic,
            "media_refs": list(entity.media_refs),
            "created_at": entity.created_at,
            "updated_at": entity.updated_at,
            "published_at": entity.published_at,
        }

    # --- 业务方法 -----------------------------------------------------

    def get_by_idempotency_key(self, key: str) -> Content | None:
        """按幂等键取内容。

        这是接入幂等的第一道查询（真正的保证在 ``ux_content_idempotency``
        唯一约束上，查询只是快速路径）。

        Args:
            key: 幂等键。

        Returns:
            内容对象；不存在返回 ``None``。
        """
        stmt = select(ContentModel).where(ContentModel.idempotency_key == key).limit(1)
        row = self._session.execute(stmt).scalars().first()
        return self._map_row_to_domain(row) if row is not None else None

    def get_by_content_hash(self, content_hash: str) -> Content | None:
        """按归一化内容哈希取内容（去重）。"""
        stmt = select(ContentModel).where(ContentModel.content_hash == content_hash).limit(1)
        row = self._session.execute(stmt).scalars().first()
        return self._map_row_to_domain(row) if row is not None else None

    def upsert_labels(
        self,
        content_id: int,
        *,
        labels: list[dict[str, Any]],
        label_raw: dict[str, Any] | None,
        label_stats: dict[str, Any] | None,
        labels_version: str,
        pipeline_version: str,
        label_ids: list[int] | None = None,
        layer_label_ids: list[int] | None = None,
        l1_code: str | None = None,
        l2_code: str | None = None,
    ) -> Content:
        """在**单事务**内写入标签相关字段。

        这五个字段必须同事务写入，否则会出现「标签与版本号不一致」的脏数据
        （C10 版本一致性校验会把它判为损坏数据并要求重打标）。

        Args:
            content_id: 内容 ID。
            labels: 最终标签列表。
            label_raw: 被过滤掉的低置信候选。
            label_stats: 分层统计与截断记录。
            labels_version: 标签体系版本。
            pipeline_version: 流水线版本六元组。
            label_ids: 全部 label_id（供 GIN 倒排）。
            layer_label_ids: 仅 L1–L4 的 label_id。
            l1_code: 一级类目 code。
            l2_code: 二级类目 code。

        Returns:
            更新后的内容对象。
        """
        values: dict[str, Any] = {
            "labels": labels,
            "label_raw": label_raw,
            "label_stats": label_stats,
            "labels_version": labels_version,
            "pipeline_version": pipeline_version,
        }
        if label_ids is not None:
            values["label_ids"] = label_ids
        if layer_label_ids is not None:
            values["layer_label_ids"] = layer_label_ids
        if l1_code is not None:
            values["l1_code"] = l1_code
        if l2_code is not None:
            values["l2_code"] = l2_code
        self.optimistic_update(content_id, values)
        refreshed = self.get(content_id)
        if refreshed is None:
            raise ValueError(f"内容 {content_id} 在标签写入后消失")
        return refreshed

    def mark_status(
        self,
        content_id: int,
        status: ContentStatus,
        *,
        quality_status: QualityStatus | None = None,
        quality_score: float | None = None,
        quality_reason: str | None = None,
        reject_reason: dict[str, Any] | None = None,
        published_at: Any = None,
    ) -> Content:
        """更新内容状态。

        Args:
            content_id: 内容 ID。
            status: 目标状态。
            quality_status: 质量状态。
            quality_score: 质量分。
            quality_reason: 质量理由。
            reject_reason: 结构化拒绝原因。
            published_at: 发布时间；进入 ``approved`` 时必须给值（DDL 的
                ``ck_content_published`` 约束）。

        Returns:
            更新后的内容对象。
        """
        values: dict[str, Any] = {"status": str(status)}
        if quality_status is not None:
            values["quality_status"] = str(quality_status)
        if quality_score is not None:
            values["quality_score"] = quality_score
        if quality_reason is not None:
            values["quality_reason"] = quality_reason
        if reject_reason is not None:
            values["reject_reason"] = reject_reason
        if published_at is not None:
            values["published_at"] = published_at
        self.optimistic_update(content_id, values)
        refreshed = self.get(content_id)
        if refreshed is None:
            raise ValueError(f"内容 {content_id} 在状态更新后消失")
        return refreshed

    def search_by_labels(
        self,
        label_ids: list[int],
        *,
        layer_label_ids: list[int] | None = None,
        status: str = "approved",
        limit: int = 100,
        offset: int = 0,
    ) -> list[Content]:
        """按标签倒排检索内容。

        这是标签偏好召回的两段式检索的第一段：先用 GIN 索引筛出候选，
        再在进程内用 CSR 精确打分（阶段 3）。

        Args:
            label_ids: 必须命中的 label_id 集合。
            layer_label_ids: 可选的层级标签过滤。
            status: 内容状态过滤；传空串表示不过滤。
            limit: 返回条数上限。
            offset: 偏移量。

        Returns:
            内容列表。
        """
        if not label_ids:
            return []
        # PostgreSQL 的数组交集运算符是 ``&&``；SQLAlchemy 的 ``.overlap()``
        # 在 Postgres 方言上不存在（那是 SQLite 的写法），必须用 ``Comparator``
        # 显式映射到 ``&&``。
        overlap = ContentModel.label_ids.op("&&")(sa.cast(label_ids, ARRAY(BigInteger)))
        stmt = select(ContentModel).where(overlap)
        if layer_label_ids:
            layer_overlap = ContentModel.layer_label_ids.op("&&")(
                sa.cast(layer_label_ids, ARRAY(BigInteger))
            )
            stmt = stmt.where(layer_overlap)
        if status:
            stmt = stmt.where(ContentModel.status == status)
        stmt = stmt.order_by(ContentModel.published_at.desc().nullslast())
        stmt = stmt.limit(limit).offset(offset)
        return [self._map_row_to_domain(row) for row in self._session.execute(stmt).scalars()]

    def list_by_status(self, status: str, *, limit: int = 100, offset: int = 0) -> list[Content]:
        """按状态分页取内容。"""
        return list(self.list(FilterSpec(status=status), limit=limit, offset=offset))
