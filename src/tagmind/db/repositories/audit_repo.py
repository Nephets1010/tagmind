"""审计仓储：LLM 审计、外发日志、实验分组、标注集读写。

事务语义（架构 §14.5）：``llm_call_id`` 唯一冲突 → 视为重试，忽略（不报错）。
这是「LLM 调用重试不会产生重复审计行」的实现。
"""

from __future__ import annotations

import hashlib
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tagmind.db.models.audit import (
    Annotation as AnnotationModel,
)
from tagmind.db.models.audit import (
    AnnotationSet as AnnotationSetModel,
)
from tagmind.db.models.audit import (
    EgressLog as EgressLogModel,
)
from tagmind.db.models.audit import (
    Experiment as ExperimentModel,
)
from tagmind.db.models.audit import (
    ExperimentAssignment as ExperimentAssignmentModel,
)
from tagmind.db.models.audit import (
    LlmCallAudit as LlmCallAuditModel,
)
from tagmind.db.repositories.base import BaseRepository, FilterSpec

__all__ = ["AuditRepository"]


class AuditRepository(BaseRepository[LlmCallAuditModel]):
    """审计与实验仓储。

    聚合根是 ``llm_call_audit``；``egress_log`` / ``experiment`` /
    ``experiment_assignment`` / ``annotation_set`` / ``annotation`` 由本仓储
    统一管理——它们都是「审计与合规」域的表，拆开反而会让事务边界破碎。
    """

    model = LlmCallAuditModel

    def __init__(self, session: Session) -> None:
        """初始化仓储。"""
        super().__init__(session)

    # --- 映射 ---------------------------------------------------------

    def _map_row_to_domain(self, row: Any) -> LlmCallAuditModel:
        """ORM 行 → ORM 行（本仓储内部直接传 ORM 对象）。"""
        return cast("LlmCallAuditModel", row)

    def _map_domain_to_values(self, entity: LlmCallAuditModel) -> dict[str, Any]:
        """ORM 行 → 列值字典。"""
        return {
            "id": entity.id,
            "llm_call_id": entity.llm_call_id,
            "trace_id": entity.trace_id,
            "content_id": entity.content_id,
            "purpose": entity.purpose,
            "provider": entity.provider,
            "model": entity.model,
            "system_prompt_version": entity.system_prompt_version,
            "user_prompt_version": entity.user_prompt_version,
            "prompt_hash": entity.prompt_hash,
            "label_def_version": entity.label_def_version,
            "tokens_in": entity.tokens_in,
            "tokens_out": entity.tokens_out,
            "latency_ms": entity.latency_ms,
            "http_status": entity.http_status,
            "retry_count": entity.retry_count,
            "cost": entity.cost,
            "degraded": entity.degraded,
            "error": entity.error,
            "pipeline_version": entity.pipeline_version,
            "created_at": entity.created_at,
        }

    # --- LLM 审计 -----------------------------------------------------

    def insert_llm_call(
        self,
        *,
        llm_call_id: str,
        trace_id: str,
        purpose: str,
        provider: str,
        model: str,
        system_prompt_version: str,
        user_prompt_version: str,
        prompt_hash: str,
        content_id: int | None = None,
        label_def_version: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        latency_ms: int = 0,
        http_status: int | None = None,
        retry_count: int = 0,
        cost: float = 0.0,
        degraded: bool = False,
        error: str | None = None,
        pipeline_version: str | None = None,
    ) -> LlmCallAuditModel | None:
        """写入一条 LLM 调用审计（幂等）。

        ``llm_call_id`` 唯一冲突时视为重试，返回 ``None`` 而不报错。

        Returns:
            新写入的行；已存在（重试）时返回 ``None``。
        """
        row = LlmCallAuditModel(
            llm_call_id=llm_call_id,
            trace_id=trace_id,
            content_id=content_id,
            purpose=purpose,
            provider=provider,
            model=model,
            system_prompt_version=system_prompt_version,
            user_prompt_version=user_prompt_version,
            prompt_hash=prompt_hash,
            label_def_version=label_def_version,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            http_status=http_status,
            retry_count=retry_count,
            cost=cost,
            degraded=degraded,
            error=error,
            pipeline_version=pipeline_version,
        )
        # SAVEPOINT 隔离：``llm_call_id`` 唯一冲突时只回滚这一次插入，
        # 不影响外层事务里已经写入的其它数据。
        savepoint = self._session.begin_nested()
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError:
            savepoint.rollback()
            return None
        savepoint.commit()
        return row

    def verify_prompt_hash(self, llm_call_id: str, prompt_hash: str) -> bool:
        """校验审计记录的 ``prompt_hash`` 是否匹配。

        不匹配说明提示词被改过而审计没更新——按 P4-LLM-AUDIT-001 必须报错，
        不能返回错版本的提示词。

        Args:
            llm_call_id: 调用 ID。
            prompt_hash: 期望的哈希。

        Returns:
            匹配返回 ``True``；不匹配或记录不存在返回 ``False``。
        """
        stmt = select(LlmCallAuditModel).where(LlmCallAuditModel.llm_call_id == llm_call_id)
        row = self._session.execute(stmt).scalars().first()
        if row is None:
            return False
        return row.prompt_hash == prompt_hash

    def list_llm_calls(self, filters: FilterSpec | None = None) -> list[LlmCallAuditModel]:
        """按条件列出 LLM 调用审计。"""
        stmt = select(LlmCallAuditModel)
        if filters:
            for column, value in filters.conditions.items():
                stmt = stmt.where(getattr(LlmCallAuditModel, column) == value)
        stmt = stmt.order_by(LlmCallAuditModel.created_at.desc())
        return list(self._session.execute(stmt).scalars())

    # --- 外发日志 -----------------------------------------------------

    def insert_egress(
        self,
        *,
        trace_id: str,
        destination: str,
        bytes_sent: int = 0,
        content_id: int | None = None,
        model: str | None = None,
        pii_masked: bool = True,
        purpose: str | None = None,
    ) -> EgressLogModel:
        """写入一条外发日志（**不含正文**）。

        Args:
            trace_id: 全链路 ID。
            destination: 目标 host[:port]。
            bytes_sent: 外发字节数。
            content_id: 内容 ID。
            model: 模型名。
            pii_masked: 外发内容是否已脱敏。
            purpose: 外发目的。

        Returns:
            落库后的行。
        """
        row = EgressLogModel(
            trace_id=trace_id,
            destination=destination,
            bytes_sent=bytes_sent,
            content_id=content_id,
            model=model,
            pii_masked=pii_masked,
            purpose=purpose,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def list_egress(self, *, limit: int = 100) -> list[EgressLogModel]:
        """列出最近的外发日志。"""
        stmt = select(EgressLogModel).order_by(EgressLogModel.created_at.desc()).limit(limit)
        return list(self._session.execute(stmt).scalars())

    # --- 实验 ---------------------------------------------------------

    def upsert_experiment(
        self,
        *,
        name: str,
        hypothesis: str | None = None,
        baseline_algo: str | None = None,
        treatment_algo: str | None = None,
        traffic_split: dict[str, Any] | None = None,
        status: str = "draft",
        metric_definitions: list[Any] | None = None,
        evidence_type: str = "offline_simulation",
    ) -> ExperimentModel:
        """创建或更新实验定义。"""
        row = (
            self._session.execute(select(ExperimentModel).where(ExperimentModel.name == name))
            .scalars()
            .first()
        )
        if row is None:
            row = ExperimentModel(name=name)
            self._session.add(row)
        row.hypothesis = hypothesis
        row.baseline_algo = baseline_algo
        row.treatment_algo = treatment_algo
        row.traffic_split = traffic_split or {"control": 0.5, "treatment": 0.5}
        row.status = status
        row.metric_definitions = metric_definitions or []
        row.evidence_type = evidence_type
        self._session.flush()
        return row

    def upsert_assignment(
        self,
        *,
        user_id: str,
        experiment_id: int,
        group: str,
        hash_salt: str,
    ) -> ExperimentAssignmentModel:
        """写入用户分组（幂等）。

        分组用 ``hash(user_id, salt)`` 稳定计算，同一用户永远落在同一组。
        """
        row = (
            self._session.execute(
                select(ExperimentAssignmentModel).where(
                    ExperimentAssignmentModel.user_id == user_id,
                    ExperimentAssignmentModel.experiment_id == experiment_id,
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            row = ExperimentAssignmentModel(
                user_id=user_id,
                experiment_id=experiment_id,
                group=group,
                hash_salt=hash_salt,
            )
            self._session.add(row)
        self._session.flush()
        return row

    def get_assignment(self, user_id: str, experiment_id: int) -> ExperimentAssignmentModel | None:
        """取某用户的分组。"""
        stmt = select(ExperimentAssignmentModel).where(
            ExperimentAssignmentModel.user_id == user_id,
            ExperimentAssignmentModel.experiment_id == experiment_id,
        )
        return self._session.execute(stmt).scalars().first()

    # --- 标注 ---------------------------------------------------------

    def upsert_annotation_set(
        self,
        *,
        version: str,
        description: str | None = None,
        sample_count: int = 500,
        label_def_version: str,
        synthetic_gold: bool = True,
        source: str = "synthetic",
        author: str | None = None,
    ) -> AnnotationSetModel:
        """创建或更新金标集。"""
        row = (
            self._session.execute(
                select(AnnotationSetModel).where(AnnotationSetModel.version == version)
            )
            .scalars()
            .first()
        )
        if row is None:
            row = AnnotationSetModel(version=version)
            self._session.add(row)
        row.description = description
        row.sample_count = sample_count
        row.label_def_version = label_def_version
        row.synthetic_gold = synthetic_gold
        row.source = source
        row.author = author
        self._session.flush()
        return row

    def insert_annotation(
        self,
        *,
        set_id: int,
        content_id: int,
        labels: list[Any],
        annotator: str,
        agreement: float | None = None,
        is_gold: bool = False,
        synthetic: bool = True,
    ) -> AnnotationModel | None:
        """写入一条标注（幂等）。

        ``ux_annotation_unique (set_id, content_id, annotator)`` 冲突时返回 ``None``。
        """
        row = AnnotationModel(
            set_id=set_id,
            content_id=content_id,
            labels=labels,
            annotator=annotator,
            agreement=agreement,
            is_gold=is_gold,
            synthetic=synthetic,
        )
        self._session.add(row)
        try:
            self._session.flush()
        except Exception:
            self._session.rollback()
            return None
        return row

    def list_annotations(self, set_id: int, *, only_gold: bool = False) -> list[AnnotationModel]:
        """列出某金标集的标注。"""
        stmt = select(AnnotationModel).where(AnnotationModel.set_id == set_id)
        if only_gold:
            stmt = stmt.where(AnnotationModel.is_gold.is_(True))
        stmt = stmt.order_by(AnnotationModel.content_id)
        return list(self._session.execute(stmt).scalars())


def compute_prompt_hash(system_prompt: str, user_prompt: str) -> str:
    """计算提示词哈希（架构 §5.7：``sha256(system||user)``）。

    Args:
        system_prompt: system 提示词。
        user_prompt: user 提示词。

    Returns:
        64 位十六进制哈希。
    """
    payload = f"{system_prompt}{user_prompt}".encode()
    return hashlib.sha256(payload).hexdigest()
