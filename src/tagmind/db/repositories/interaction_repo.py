"""交互仓储：曝光写入（批量）、画像读写、热门榜窗口聚合。

事务语义（架构 §14.5）：``bulk_insert_exposures`` 单事务批量写，
``ON CONFLICT DO NOTHING`` 兜底——曝光日志允许重复（同一请求的多次上报），
但不能因为重复上报而让整批失败。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from tagmind.db.models.interaction import (
    ExposureLog as ExposureLogModel,
)
from tagmind.db.models.interaction import (
    UserProfile as UserProfileModel,
)
from tagmind.db.repositories.base import BaseRepository, FilterSpec
from tagmind.domain.interaction import BehaviorAction, ExposureEvent
from tagmind.domain.profile import TagWeights, UserProfile

__all__ = ["InteractionRepository"]


class InteractionRepository(BaseRepository[UserProfile]):
    """交互与画像仓储。

    聚合根是 ``user_profile``；``exposure_log`` 由本仓储统一管理，
    因为「写曝光 → 更新画像」经常需要在同一事务里完成。
    """

    model = UserProfileModel

    def __init__(self, session: Session) -> None:
        """初始化仓储。"""
        super().__init__(session)

    # --- 映射 ---------------------------------------------------------

    def _map_row_to_domain(self, row: Any) -> UserProfile:
        """ORM 行 → 领域对象。"""
        weights = {
            int(key): float(value)
            for key, value in (row.tag_weights or {}).items()
            if _is_number(value)
        }
        affinity = {
            int(key): float(value)
            for key, value in (row.label_affinity or {}).items()
            if _is_number(value)
        }
        return UserProfile(
            user_id=row.user_id,
            tag_weights=TagWeights(weights=weights, affinity=affinity),
            cold_start=row.cold_start,
            confidence=float(row.confidence),
            behavior_count=row.behavior_count,
            label_dims=row.label_dims,
            decay_lambda=float(row.decay_lambda),
            last_behavior_at=row.last_behavior_at,
            drift_detected=row.drift_detected,
            drift_score=float(row.drift_score) if row.drift_score is not None else None,
            model_version=row.model_version,
            tag_space_version=row.tag_space_version,
            label_weight_norm=row.label_weight_norm,
            cold_start_threshold=row.cold_start_threshold,
            weights_sum=row.weights_sum,
            synthetic=row.synthetic,
            updated_at=row.updated_at,
        )

    def _map_domain_to_values(self, entity: UserProfile) -> dict[str, Any]:
        """领域对象 → 列值字典。"""
        return {
            "user_id": entity.user_id,
            "tag_weights": entity.to_jsonb_weights(),
            "label_affinity": entity.to_jsonb_affinity(),
            "cold_start": entity.cold_start,
            "confidence": entity.confidence,
            "behavior_count": entity.behavior_count,
            "label_dims": entity.label_dims,
            "decay_lambda": entity.decay_lambda,
            "last_behavior_at": entity.last_behavior_at,
            "drift_detected": entity.drift_detected,
            "drift_score": entity.drift_score,
            "model_version": entity.model_version,
            "tag_space_version": entity.tag_space_version,
            "label_weight_norm": entity.label_weight_norm,
            "cold_start_threshold": entity.cold_start_threshold,
            "weights_sum": entity.weights_sum,
            "synthetic": entity.synthetic,
            "updated_at": entity.updated_at,
        }

    # --- 曝光 ---------------------------------------------------------

    def bulk_insert_exposures(self, events: list[ExposureEvent]) -> int:
        """批量写入曝光日志（幂等）。

        使用 ``ON CONFLICT DO NOTHING``：同一请求的重复上报被忽略，
        不报错也不重复计数。

        Args:
            events: 曝光事件列表。

        Returns:
            实际新写入的行数。
        """
        if not events:
            return 0
        payload = [
            {
                "user_id": event.user_id,
                "content_id": event.content_id,
                "action": str(event.action),
                "ts": event.ts,
                "dwell_ms": event.dwell_ms,
                "request_id": event.request_id,
                "rank_pos": event.rank_pos,
                "score": event.score,
                "algo": event.algo,
                "recall_source": event.recall_source,
                "exp_group": event.exp_group,
                "request_latency_ms": event.request_latency_ms,
                "synthetic": event.synthetic,
                "pipeline_version": event.pipeline_version,
            }
            for event in events
        ]
        insert_stmt = pg_insert(ExposureLogModel).values(payload)
        conflict_stmt = insert_stmt.on_conflict_do_nothing(
            index_elements=["request_id", "content_id", "action", "ts"]
        )
        # 用 RETURNING 精确统计**实际插入**的行数：``rowcount`` 在
        # ``ON CONFLICT DO NOTHING`` 下可能返回 -1（psycopg 对多行 VALUES
        # 的计数不总是可靠），而 -1 会被误当成「插入了 -1 行」。
        # ``returning()`` 的静态类型是 ReturningInsert，与 Insert 不兼容，
        # 因此必须换一个变量名承接，不能原地赋回 stmt。
        returning_stmt = conflict_stmt.returning(ExposureLogModel.id)
        result = cast("CursorResult[Any]", self._session.execute(returning_stmt))
        inserted = len(result.fetchall())
        self._session.flush()
        return inserted

    def count_exposures(self, filters: FilterSpec | None = None) -> int:
        """按条件统计曝光条数。"""
        stmt = select(func.count()).select_from(ExposureLogModel)
        if filters:
            for column, value in filters.conditions.items():
                stmt = stmt.where(getattr(ExposureLogModel, column) == value)
        return int(self._session.execute(stmt).scalar_one())

    def hot_items(
        self, window_start: datetime, window_end: datetime, *, limit: int = 100
    ) -> list[tuple[int, int]]:
        """热门榜：按窗口内的有效交互数排序。

        **只统计有效交互**（``click`` / ``dwell``）——``view`` 与 ``skip``
        不计入（歧义 A-09）。

        Args:
            window_start: 窗口起。
            window_end: 窗口止。
            limit: 返回条数。

        Returns:
            ``(content_id, 交互数)`` 列表，按交互数降序。
        """
        stmt = (
            select(
                ExposureLogModel.content_id,
                func.count().label("interactions"),
            )
            .where(ExposureLogModel.ts >= window_start)
            .where(ExposureLogModel.ts < window_end)
            .where(
                ExposureLogModel.action.in_([str(BehaviorAction.CLICK), str(BehaviorAction.DWELL)])
            )
            .group_by(ExposureLogModel.content_id)
            .order_by(func.count().desc())
            .limit(limit)
        )
        return [(int(row.content_id), int(row.interactions)) for row in self._session.execute(stmt)]

    def effective_interaction_count(self, user_id: str) -> int:
        """某用户的有效交互总数。"""
        stmt = (
            select(func.count())
            .select_from(ExposureLogModel)
            .where(
                ExposureLogModel.user_id == user_id,
                ExposureLogModel.action.in_([str(BehaviorAction.CLICK), str(BehaviorAction.DWELL)]),
            )
        )
        return int(self._session.execute(stmt).scalar_one())

    def behavior_count_for(self, user_id: str) -> float:
        """某用户的 ``behavior_count``（按 A-09 口径：view 计 0.5）。"""
        rows = self._session.execute(
            select(ExposureLogModel.action, func.count())
            .where(ExposureLogModel.user_id == user_id)
            .group_by(ExposureLogModel.action)
        ).all()
        total = 0.0
        for action, count in rows:
            try:
                total += _behavior_weight(action) * int(count)
            except ValueError:
                continue
        return total

    # --- 画像 ---------------------------------------------------------

    def get_profile(self, user_id: str) -> UserProfile | None:
        """取某用户的画像。"""
        row = self._session.get(UserProfileModel, user_id)
        return self._map_row_to_domain(row) if row is not None else None

    def save_profile(self, profile: UserProfile) -> UserProfile:
        """保存画像（新增或更新）。

        Args:
            profile: 画像对象。

        Returns:
            落库后的画像。
        """
        values = self._map_domain_to_values(profile)
        row = self._session.get(UserProfileModel, profile.user_id)
        if row is None:
            row = UserProfileModel(user_id=profile.user_id)
            self._session.add(row)
        for column, value in values.items():
            if column == "user_id":
                continue
            setattr(row, column, value)
        self._session.flush()
        return self._map_row_to_domain(row)

    def list_profiles(self, *, limit: int = 100, offset: int = 0) -> list[UserProfile]:
        """分页列出画像。"""
        stmt = (
            select(UserProfileModel)
            .order_by(UserProfileModel.updated_at.desc().nullslast())
            .limit(limit)
            .offset(offset)
        )
        return [self._map_row_to_domain(row) for row in self._session.execute(stmt).scalars()]

    def count_profiles(self, filters: FilterSpec | None = None) -> int:
        """按条件统计画像数。"""
        stmt = select(func.count()).select_from(UserProfileModel)
        if filters:
            for column, value in filters.conditions.items():
                stmt = stmt.where(getattr(UserProfileModel, column) == value)
        return int(self._session.execute(stmt).scalar_one())

    def recompute_and_verify(self, user_id: str) -> bool:
        """重算并校验画像自洽性。

        对应架构 §13.2 的 V6 / V8 / V9。

        Args:
            user_id: 用户 ID。

        Returns:
            自洽返回 ``True``。
        """
        profile = self.get_profile(user_id)
        if profile is None:
            return False
        return profile.is_consistent()

    def partition_names(self) -> list[str]:
        """列出 ``exposure_log`` 的分区名（运维巡检用）。"""
        stmt = text(
            """
            SELECT c.relname
            FROM pg_class c
            JOIN pg_inherits i ON i.inhrelid = c.oid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = 'exposure_log'
            ORDER BY c.relname
            """
        )
        return [str(row[0]) for row in self._session.execute(stmt)]


def _is_number(value: Any) -> bool:
    """判断 JSONB 值是否为数值。"""
    return isinstance(value, int | float) and not isinstance(value, bool)


def _behavior_weight(action: str) -> float:
    """动作对 ``behavior_count`` 的贡献权重。"""
    from tagmind.domain.interaction import effective_interaction_weight

    return effective_interaction_weight(BehaviorAction(action))
