"""``BaseRepository`` 主键适配回归测试（QA 报告 D-03）。

阶段 0 把主键写死成 ``id``，导致 4 张表确定性崩溃：

============ =========================== =====================================
表            主键                        失败现象
============ =========================== =====================================
user_profile  ``user_id`` (str)           ``list()`` AttributeError、
                                          ``upsert()`` TypeError
content_vector ``content_id``             ``list()`` AttributeError
experiment_assignment ``user_id, experiment_id`` 复合主键
exposure_log  ``id, ts`` 复合主键         ``_primary_key_name()`` DatabaseError
============ =========================== =====================================

本文件用轻量假 Session 覆盖这些路径，**不需要 PostgreSQL**。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Column

import tagmind.db.models  # noqa: F401 — 触发 ORM 注册
from tagmind.core.errors import DatabaseError
from tagmind.db.base import Base
from tagmind.db.models.audit import ExperimentAssignment
from tagmind.db.models.interaction import ExposureLog, UserProfile
from tagmind.db.models.vector import ContentVector
from tagmind.db.repositories.base import BaseRepository, bulk_delete
from tagmind.db.repositories.interaction_repo import InteractionRepository
from tagmind.db.repositories.vector_repo import VectorRepository
from tagmind.domain.profile import UserProfile as UserProfileDomain
from tagmind.domain.vector import DenseVector


class _FakeResult:
    """模拟 SQLAlchemy ``Result``。"""

    def __init__(self, rows: list[Any] | None = None, *, rowcount: int = 1) -> None:
        self._rows = list(rows or [])
        self.rowcount = rowcount

    def scalars(self) -> _FakeResult:
        return self

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalar_one(self) -> Any:
        return self._rows[0] if self._rows else 0

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def __iter__(self) -> Any:
        return iter(self._rows)


class _FakeSession:
    """记录语句与新增行的假 Session（复用 test_repositories_unit.py 的思路）。"""

    def __init__(self, *, rowcount: int = 1, version_row: tuple[Any, ...] | None = None) -> None:
        self.calls: list[Any] = []
        self.added: list[Any] = []
        self.row_by_pk: dict[Any, Any] = {}
        self.rowcount = rowcount
        self.version_row = version_row

    def execute(self, statement: Any, *args: Any, **kwargs: Any) -> _FakeResult:
        self.calls.append(statement)
        rows: list[Any] = []
        if self.version_row is not None:
            rows = [self.version_row]
        return _FakeResult(rows, rowcount=self.rowcount)

    def get(self, model: Any, pk: Any) -> Any:
        return self.row_by_pk.get(pk)

    def add(self, entity: Any) -> None:
        self.added.append(entity)

    def flush(self) -> None:
        return None

    def delete(self, row: Any) -> None:
        return None

    def begin_nested(self) -> Any:
        class _Savepoint:
            def rollback(self) -> None:
                return None

            def commit(self) -> None:
                return None

        return _Savepoint()


class _GenericRepo(BaseRepository[Any]):
    """只用于验证基类自身行为的通用仓储。"""

    def __init__(self, model: type[Any], session: Any) -> None:
        super().__init__(session)
        self.model = model

    def _map_row_to_domain(self, row: Any) -> Any:
        return row

    def _map_domain_to_values(self, entity: Any) -> dict[str, Any]:
        return dict(entity)


def _profile(**overrides: Any) -> UserProfileDomain:
    defaults: dict[str, Any] = {
        "user_id": "u-1",
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return UserProfileDomain(**defaults)


def _vector(**overrides: Any) -> DenseVector:
    defaults: dict[str, Any] = {
        "content_id": 1,
        "embedding": (0.1, 0.2),
        "embedding_model": "test",
        "embedding_version": "v1",
    }
    defaults.update(overrides)
    return DenseVector(**defaults)


#: 4 张「非 id 单列主键 / 复合主键」表。
PROBLEM_MODELS: tuple[tuple[str, type[Any], tuple[str, ...]], ...] = (
    ("user_profile", UserProfile, ("user_id",)),
    ("content_vector", ContentVector, ("content_id",)),
    ("experiment_assignment", ExperimentAssignment, ("user_id", "experiment_id")),
    ("exposure_log", ExposureLog, ("id", "ts")),
)


class TestPrimaryKeyMetadata:
    """主键派生必须与 ORM metadata 一致。"""

    @pytest.mark.parametrize(("table_name", "model", "expected"), PROBLEM_MODELS)
    def test_pk_names_match_ddl(
        self, table_name: str, model: type[Any], expected: tuple[str, ...]
    ) -> None:
        repo = _GenericRepo(model, _FakeSession())
        assert repo._pk_names() == expected
        ddl_names = tuple(str(c.name) for c in Base.metadata.tables[table_name].primary_key.columns)
        assert repo._pk_names() == ddl_names

    @pytest.mark.parametrize(("table_name", "model", "expected"), PROBLEM_MODELS)
    def test_pk_columns_are_column_objects(
        self, table_name: str, model: type[Any], expected: tuple[str, ...]
    ) -> None:
        repo = _GenericRepo(model, _FakeSession())
        columns = repo._pk_columns()
        assert len(columns) == len(expected)
        assert all(isinstance(column, Column) for column in columns)

    def test_single_pk_name(self) -> None:
        assert _GenericRepo(UserProfile, _FakeSession())._primary_key_name() == "user_id"
        assert _GenericRepo(ContentVector, _FakeSession())._primary_key_name() == "content_id"

    @pytest.mark.parametrize("model", [ExperimentAssignment, ExposureLog])
    def test_composite_pk_is_rejected_with_actionable_message(self, model: type[Any]) -> None:
        repo = _GenericRepo(model, _FakeSession())
        with pytest.raises(DatabaseError) as excinfo:
            repo._primary_key_name()
        assert "复合主键请使用专用仓储方法" in excinfo.value.message
        assert excinfo.value.details["pk_columns"] == repo._pk_names()


class TestListDoesNotAssumeId:
    """D-03 #1：``list()`` 默认排序不能假设 ``model.id`` 存在。"""

    @pytest.mark.parametrize(("table_name", "model", "expected"), PROBLEM_MODELS)
    def test_list_no_attribute_error(
        self, table_name: str, model: type[Any], expected: tuple[str, ...]
    ) -> None:
        session = _FakeSession()
        repo = _GenericRepo(model, session)
        assert repo.list() == []
        assert session.calls, "必须真的构造并发出查询"

    def test_list_uses_real_pk_for_user_profile(self) -> None:
        session = _FakeSession()
        repo = InteractionRepository(session)  # type: ignore[arg-type]
        assert repo.list() == []
        statement = str(session.calls[-1])
        assert "user_profile.user_id" in statement
        assert "ORDER BY" in statement

    def test_list_uses_real_pk_for_content_vector(self) -> None:
        session = _FakeSession()
        repo = VectorRepository(session)  # type: ignore[arg-type]
        assert repo.list() == []
        statement = str(session.calls[-1])
        assert "content_vector.content_id" in statement

    def test_list_composite_pk_orders_by_both_columns(self) -> None:
        session = _FakeSession()
        repo = _GenericRepo(ExposureLog, session)
        repo.list()
        statement = str(session.calls[-1])
        assert "exposure_log.id" in statement
        assert "exposure_log.ts" in statement

    def test_list_respects_explicit_order_by(self) -> None:
        session = _FakeSession()
        repo = _GenericRepo(UserProfile, session)
        repo.list(order_by=UserProfile.confidence)
        assert "confidence" in str(session.calls[-1])

    def test_list_applies_filters(self) -> None:
        session = _FakeSession()
        repo = _GenericRepo(UserProfile, session)
        assert repo.list(filters=None) == []
        from tagmind.db.repositories.base import FilterSpec

        repo.list(filters=FilterSpec(cold_start=True))
        assert "cold_start" in str(session.calls[-1])


class TestUpsertStringPrimaryKey:
    """D-03 #5/#6：字符串主键不能做 ``<= 0``，构造行不能写死 ``id=``。"""

    def test_string_pk_upsert_does_not_raise(self) -> None:
        session = _FakeSession()
        repo = InteractionRepository(session)  # type: ignore[arg-type]
        result = repo.upsert(_profile(user_id="u-1"))
        assert result.user_id == "u-1"
        assert session.added, "必须真的构造一行"

    def test_string_pk_row_uses_user_id_kwarg(self) -> None:
        session = _FakeSession()
        repo = InteractionRepository(session)  # type: ignore[arg-type]
        repo.upsert(_profile(user_id="u-42"))
        row = session.added[0]
        assert isinstance(row, UserProfile)
        assert row.user_id == "u-42"

    def test_empty_string_pk_treated_as_new(self) -> None:
        """空串主键不再触发 ``<= 0`` 的 TypeError；走 INSERT 由数据库兜底。"""
        session = _FakeSession()
        repo = InteractionRepository(session)  # type: ignore[arg-type]
        repo.upsert(_profile(user_id=""))
        assert len(session.added) == 1
        assert isinstance(session.added[0], UserProfile)

    def test_non_id_integer_pk_uses_content_id_kwarg(self) -> None:
        session = _FakeSession()
        repo = VectorRepository(session)  # type: ignore[arg-type]
        result = repo.upsert(_vector(content_id=7))
        assert result.content_id == 7
        assert isinstance(session.added[0], ContentVector)
        assert session.added[0].content_id == 7

    def test_zero_int_pk_is_treated_as_new(self) -> None:
        """Content.id=0 仍是「未落库」，原有语义必须保留。"""
        from tagmind.db.repositories.content_repo import ContentRepository
        from tagmind.domain.content import Content

        session = _FakeSession()
        repo = ContentRepository(session)  # type: ignore[arg-type]
        entity = Content(id=0, raw_text="x", clean_text="x", pipeline_version="v")
        repo.upsert(entity)
        assert len(session.added) == 1, "id=0 表示新增，必须构造一行 INSERT"
        # 主键不显式赋值，交给数据库的 identity 生成。
        assert session.added[0].id is None

    def test_existing_row_is_updated_in_place(self) -> None:
        session = _FakeSession()
        existing = UserProfile(user_id="u-1")
        session.row_by_pk["u-1"] = existing
        repo = InteractionRepository(session)  # type: ignore[arg-type]
        repo.upsert(_profile(user_id="u-1", confidence=0.5))
        assert existing.confidence == 0.5
        assert session.added == []


class TestIsNewEntityId:
    """按 Python 类型区分「未落库」，禁止对字符串做数值比较。"""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, True),
            ("", True),
            (0, True),
            (-1, True),
            ("u-1", False),
            (1, False),
            (True, False),
            (datetime(2026, 1, 1, tzinfo=UTC), False),
        ],
    )
    def test_classification(self, value: Any, expected: bool) -> None:
        assert BaseRepository._is_new_entity_id(value) is expected


class TestPkCondition:
    """复合主键用 ``and_`` 组合，且不接受残缺键值。"""

    def test_single_pk_condition(self) -> None:
        repo = _GenericRepo(UserProfile, _FakeSession())
        condition = repo._pk_condition("u-1")
        assert "user_profile.user_id" in str(condition)

    def test_composite_pk_condition_uses_and(self) -> None:
        repo = _GenericRepo(ExposureLog, _FakeSession())
        condition = repo._pk_condition((1, datetime(2026, 1, 1, tzinfo=UTC)))
        rendered = str(condition)
        assert "exposure_log.id" in rendered
        assert "exposure_log.ts" in rendered
        assert "AND" in rendered

    def test_composite_pk_rejects_scalar(self) -> None:
        repo = _GenericRepo(ExposureLog, _FakeSession())
        with pytest.raises(DatabaseError) as excinfo:
            repo._pk_condition(1)
        assert "复合主键请使用专用仓储方法" in excinfo.value.message
        assert excinfo.value.details["values_required"] == 2

    def test_composite_pk_rejects_wrong_arity(self) -> None:
        repo = _GenericRepo(ExperimentAssignment, _FakeSession())
        with pytest.raises(DatabaseError):
            repo._pk_condition(("u-1", 1, "extra"))

    def test_optimistic_update_uses_pk_condition(self) -> None:
        """D-03 #2/#3：无 id 列的表调 optimistic_update 不能抛 AttributeError。"""
        session = _FakeSession(version_row=("2026-01-01T00:00:00Z",))
        repo = InteractionRepository(session)  # type: ignore[arg-type]
        assert repo.optimistic_update("u-1", {"confidence": 0.8}) is True
        assert len(session.calls) == 2  # 版本查询 + 条件更新
        assert "user_profile.user_id" in str(session.calls[-1])

    def test_optimistic_update_on_composite_pk(self) -> None:
        session = _FakeSession(version_row=("2026-01-01T00:00:00Z",))
        repo = _GenericRepo(ExposureLog, session)
        key = (1, datetime(2026, 1, 1, tzinfo=UTC))
        assert (
            repo.optimistic_update(key, {"score": 1.0}, expected_version_column="created_at")
            is True
        )


class TestBulkDelete:
    """D-03 #4：``bulk_delete`` 不能假设 ``model.id`` 存在。"""

    def test_uses_dynamic_pk_column(self) -> None:
        session = _FakeSession()
        assert bulk_delete(UserProfile, session, ["u-1", "u-2"]) == 1
        assert "user_profile.user_id" in str(session.calls[-1])

    def test_content_vector_model(self) -> None:
        session = _FakeSession()
        assert bulk_delete(ContentVector, session, [1, 2]) == 1
        assert "content_vector.content_id" in str(session.calls[-1])

    def test_empty_ids_short_circuits(self) -> None:
        session = _FakeSession()
        assert bulk_delete(UserProfile, session, []) == 0
        assert session.calls == []

    def test_composite_pk_rejected_explicitly(self) -> None:
        session = _FakeSession()
        with pytest.raises(DatabaseError) as excinfo:
            bulk_delete(ExposureLog, session, [1])
        assert "复合主键请使用专用仓储方法" in excinfo.value.message
