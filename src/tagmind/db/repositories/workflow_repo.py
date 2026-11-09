"""工作流仓储：冲突日志、复核任务/轨迹、漂移记录。

事务语义（架构 §14.5）：``review_record`` 只 ``INSERT``，
``UPDATE`` / ``DELETE`` 被数据库触发器拒绝——这是**数据层**的不可变保证，
应用层只是遵守它。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tagmind.db.models.workflow import (
    LabelConflictLog as LabelConflictLogModel,
)
from tagmind.db.models.workflow import (
    LabelDriftCheck as LabelDriftCheckModel,
)
from tagmind.db.models.workflow import (
    ReviewRecord as ReviewRecordModel,
)
from tagmind.db.models.workflow import (
    ReviewTask as ReviewTaskModel,
)
from tagmind.db.repositories.base import BaseRepository, FilterSpec
from tagmind.domain.conflict import (
    ArbitrationResult,
    ConflictCandidate,
    ConflictLog,
    ConflictType,
    Evidence,
    ReviewStatus,
)
from tagmind.domain.quality import QualityVerdict

__all__ = ["WorkflowRepository"]


class WorkflowRepository(BaseRepository[ConflictLog]):
    """冲突与复核仓储。

    聚合根是 ``label_conflict_log``；``review_task`` / ``review_record`` /
    ``label_drift_check`` 作为同一工作流的附属表由本仓储统一管理，
    避免它们被拆到多个仓储导致事务边界破碎。
    """

    model = LabelConflictLogModel

    def __init__(self, session: Session) -> None:
        """初始化仓储。"""
        super().__init__(session)

    # --- 映射 ---------------------------------------------------------

    def _map_row_to_domain(self, row: Any) -> ConflictLog:
        """ORM 行 → 领域对象。"""
        candidates = tuple(
            ConflictCandidate(
                label_id=int(item.get("label_id", 0)),
                code=str(item.get("code", "")),
                layer=int(item.get("layer", 0)),
                confidence=float(item.get("confidence", 0.0)),
                source=str(item.get("source", "rule")),
                name=str(item.get("name", "")),
                reason=str(item.get("reason", "")),
                rule_id=item.get("rule_id"),
            )
            for item in (row.candidates or ())
            if isinstance(item, dict)
        )
        evidence = tuple(
            Evidence(
                sample_id=str(item.get("sample_id", "")),
                score=float(item.get("score", 0.0)),
                matched_label=str(item.get("matched_label", "")),
                source=str(item.get("source", "rag")),
                snippet=str(item.get("snippet", "")),
            )
            for item in (row.evidence_detail or ())
            if isinstance(item, dict)
        )
        return ConflictLog(
            id=row.id,
            content_id=row.content_id,
            candidates=candidates,
            conflict_type=ConflictType(row.conflict_type),
            detected_by=row.detected_by,
            review_status=ReviewStatus(row.review_status),
            resolved=tuple(row.resolved) if row.resolved else None,
            reason=row.reason,
            retrieved_evidence_ids=tuple(row.retrieved_evidence_ids or ()),
            retrieved_scores=row.retrieved_scores,
            arbitrator_model=row.arbitrator_model,
            arbitrator_prompt_version=row.arbitrator_prompt_version,
            confidence=float(row.confidence) if row.confidence is not None else None,
            reviewer=row.reviewer,
            reviewed_at=row.reviewed_at,
            resolved_before=tuple(row.resolved_before) if row.resolved_before else None,
            rule_hits=tuple(row.rule_hits or ()),
            evidence_detail=evidence,
            pipeline_version=row.pipeline_version,
            label_def_version=row.label_def_version,
            model_version=row.model_version,
            prompt_version=row.prompt_version,
            trace_id=row.trace_id,
            created_at=row.created_at,
        )

    def _map_domain_to_values(self, entity: ConflictLog) -> dict[str, Any]:
        """领域对象 → 列值字典。"""
        return {
            "id": entity.id,
            "content_id": entity.content_id,
            "candidates": [candidate.to_dict() for candidate in entity.candidates],
            "resolved": list(entity.resolved) if entity.resolved else None,
            "reason": entity.reason,
            "conflict_type": str(entity.conflict_type),
            "detected_by": entity.detected_by,
            "retrieved_evidence_ids": list(entity.retrieved_evidence_ids) or None,
            "retrieved_scores": entity.retrieved_scores,
            "arbitrator_model": entity.arbitrator_model,
            "arbitrator_prompt_version": entity.arbitrator_prompt_version,
            "confidence": entity.confidence,
            "review_status": str(entity.review_status),
            "reviewer": entity.reviewer,
            "reviewed_at": entity.reviewed_at,
            "resolved_before": list(entity.resolved_before) if entity.resolved_before else None,
            "rule_hits": list(entity.rule_hits),
            "evidence_detail": [evidence.to_dict() for evidence in entity.evidence_detail],
            "pipeline_version": entity.pipeline_version,
            "label_def_version": entity.label_def_version,
            "model_version": entity.model_version,
            "prompt_version": entity.prompt_version,
            "trace_id": entity.trace_id,
            "created_at": entity.created_at,
        }

    # --- 冲突日志 -----------------------------------------------------

    def insert_conflict(
        self,
        *,
        content_id: int,
        conflict_type: ConflictType,
        detected_by: str,
        candidates: list[dict[str, Any]],
        rule_hits: list[dict[str, Any]] | None = None,
        evidence_detail: list[dict[str, Any]] | None = None,
        pipeline_version: str | None = None,
        label_def_version: str | None = None,
        model_version: str | None = None,
        prompt_version: str | None = None,
        trace_id: str | None = None,
    ) -> ConflictLog:
        """写入一条冲突日志。

        Args:
            content_id: 内容 ID。
            conflict_type: 冲突类型。
            detected_by: 检出方（``rule`` / ``llm`` / ``human``）。
            candidates: 候选标签列表。
            rule_hits: 规则命中明细。
            evidence_detail: 证据快照。
            pipeline_version: 流水线版本。
            label_def_version: 标签体系版本。
            model_version: 模型版本。
            prompt_version: 提示词版本。
            trace_id: 全链路 ID。

        Returns:
            落库后的冲突日志。
        """
        entity = ConflictLog(
            id=0,
            content_id=content_id,
            candidates=(),
            conflict_type=conflict_type,
            detected_by=detected_by,
            rule_hits=tuple(rule_hits or ()),
            evidence_detail=(),
            pipeline_version=pipeline_version,
            label_def_version=label_def_version,
            model_version=model_version,
            prompt_version=prompt_version,
            trace_id=trace_id,
        )
        values = self._map_domain_to_values(entity)
        values.pop("id", None)
        values["candidates"] = candidates
        values["evidence_detail"] = evidence_detail or []
        row = LabelConflictLogModel(**values)
        self._session.add(row)
        self._session.flush()
        return self._map_row_to_domain(row)

    def record_arbitration(
        self,
        conflict_id: int,
        result: ArbitrationResult,
        *,
        reviewer: str | None = None,
    ) -> ConflictLog:
        """把仲裁结论写回冲突日志。

        Args:
            conflict_id: 冲突日志 ID。
            result: 仲裁结论。
            reviewer: 复核人（转人工时填）。

        Returns:
            更新后的冲突日志。
        """
        values: dict[str, Any] = {
            "resolved": list(result.resolved_labels),
            "reason": result.reason,
            "arbitrator_model": result.arbitrator_model,
            "arbitrator_prompt_version": result.prompt_version,
            "confidence": result.confidence,
            "review_status": str(result.review_status),
            "retrieved_evidence_ids": list(result.retrieved_evidence_ids) or None,
        }
        if reviewer is not None:
            values["reviewer"] = reviewer
        self.optimistic_update(conflict_id, values)
        refreshed = self.get(conflict_id)
        if refreshed is None:
            raise ValueError(f"冲突日志 {conflict_id} 在仲裁写入后消失")
        return refreshed

    def list_pending_conflicts(self, *, limit: int = 100) -> list[ConflictLog]:
        """列出待复核的冲突。"""
        stmt = (
            select(LabelConflictLogModel)
            .where(LabelConflictLogModel.review_status == ReviewStatus.PENDING.value)
            .order_by(LabelConflictLogModel.created_at.desc())
            .limit(limit)
        )
        return [self._map_row_to_domain(row) for row in self._session.execute(stmt).scalars()]

    # --- 复核任务 -----------------------------------------------------

    def create_review_task(
        self,
        *,
        task_type: str,
        target_type: str,
        target_id: int,
        priority: int = 5,
        reason: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> ReviewTaskModel:
        """创建复核任务（幂等）。

        ``ux_review_task_target`` 保证同一目标同一类型只有一个任务。

        Returns:
            落库后的任务行。
        """
        # 用 SAVEPOINT 隔离：唯一约束冲突时只回滚这一次插入，
        # 不会把外层事务（可能已写了别的东西）一起废掉。
        savepoint = self._session.begin_nested()
        task = ReviewTaskModel(
            task_type=task_type,
            target_type=target_type,
            target_id=target_id,
            priority=priority,
            status="pending",
            reason=reason,
            detail=detail or {},
        )
        self._session.add(task)
        try:
            self._session.flush()
        except IntegrityError:
            savepoint.rollback()
            stmt = (
                select(ReviewTaskModel)
                .where(
                    ReviewTaskModel.task_type == task_type,
                    ReviewTaskModel.target_type == target_type,
                    ReviewTaskModel.target_id == target_id,
                )
                .limit(1)
            )
            existing = self._session.execute(stmt).scalars().first()
            if existing is None:
                raise
            return existing
        savepoint.commit()
        return task

    def append_review_record(
        self,
        *,
        task_id: int,
        reviewer: str,
        action: str,
        content_id: int | None = None,
        before_json: dict[str, Any] | None = None,
        after_json: dict[str, Any] | None = None,
        comment: str | None = None,
        trace_id: str | None = None,
    ) -> ReviewRecordModel:
        """追加一条复核轨迹。

        这是**只 insert** 的操作；``UPDATE`` / ``DELETE`` 会被数据库触发器拒绝。

        Returns:
            落库后的轨迹行。
        """
        record = ReviewRecordModel(
            task_id=task_id,
            content_id=content_id,
            reviewer=reviewer,
            action=action,
            before_json=before_json,
            after_json=after_json,
            comment=comment,
            trace_id=trace_id,
        )
        self._session.add(record)
        self._session.flush()
        return record

    def list_review_records(self, task_id: int) -> list[ReviewRecordModel]:
        """列出某任务的全部轨迹。"""
        stmt = (
            select(ReviewRecordModel)
            .where(ReviewRecordModel.task_id == task_id)
            .order_by(ReviewRecordModel.created_at)
        )
        return list(self._session.execute(stmt).scalars())

    # --- 漂移 ---------------------------------------------------------

    def upsert_drift_check(
        self,
        *,
        check_week: str,
        sample_size: int = 200,
        consistency_rate: float | None = None,
        drift_detected: bool = False,
        low_support_labels: list[Any] | None = None,
        fed_back_examples: list[Any] | None = None,
        reviewer: str | None = None,
        pipeline_version: str | None = None,
        label_def_version: str | None = None,
        sampling_method: str = "random_stratified_l1",
        manual_filled: bool = False,
    ) -> LabelDriftCheckModel:
        """写入 / 更新某周的漂移检查结果。

        Q20 边界：``manual_filled=False`` 时 ``consistency_rate`` 必须为 ``None``，
        不得用模型自评代替人工一致率。

        Returns:
            落库后的漂移检查行。
        """
        existing_stmt = select(LabelDriftCheckModel).where(
            LabelDriftCheckModel.check_week == check_week
        )
        row = self._session.execute(existing_stmt).scalars().first()
        if row is None:
            row = LabelDriftCheckModel(check_week=check_week)
            self._session.add(row)
        row.sample_size = sample_size
        row.consistency_rate = consistency_rate if manual_filled else None
        row.drift_detected = drift_detected
        row.low_support_labels = low_support_labels or []
        row.fed_back_examples = fed_back_examples or []
        row.reviewer = reviewer
        row.pipeline_version = pipeline_version
        row.label_def_version = label_def_version
        row.sampling_method = sampling_method
        row.manual_filled = manual_filled
        self._session.flush()
        return row

    def list_drift_checks(self, *, limit: int = 52) -> list[LabelDriftCheckModel]:
        """列出最近的漂移检查记录。"""
        stmt = (
            select(LabelDriftCheckModel)
            .order_by(LabelDriftCheckModel.check_week.desc())
            .limit(limit)
        )
        return list(self._session.execute(stmt).scalars())

    def list_review_tasks(self, filters: FilterSpec | None = None) -> list[ReviewTaskModel]:
        """按条件列出复核任务。"""
        stmt = select(ReviewTaskModel)
        if filters:
            for column, value in filters.conditions.items():
                stmt = stmt.where(getattr(ReviewTaskModel, column) == value)
        stmt = stmt.order_by(ReviewTaskModel.created_at.desc())
        return list(self._session.execute(stmt).scalars())

    def record_quality_trail(
        self,
        *,
        content_id: int,
        verdict: QualityVerdict,
        task_id: int | None = None,
    ) -> None:
        """把质量判定追加到复核轨迹。

        Args:
            content_id: 内容 ID。
            verdict: 质量结论。
            task_id: 关联的复核任务；``None`` 时跳过（无任务可挂）。
        """
        if task_id is None:
            return
        self.append_review_record(
            task_id=task_id,
            reviewer="system:quality_gate",
            action="modify",
            content_id=content_id,
            after_json=verdict.to_payload(),
            comment=verdict.reason,
        )
