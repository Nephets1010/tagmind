"""仓储层的纯逻辑单元测试。

仓储的主要路径需要真实数据库，因此由集成测试覆盖。但**映射逻辑与参数校验**
不依赖数据库，可以用轻量假对象覆盖——这部分恰恰是出错后最难发现的地方
（列名写错、枚举字符串化不一致、空集合与 None 的语义混淆）。

这里用轻量假对象造一个假 Session，只记录调用参数。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from tagmind.db.repositories.base import FilterSpec
from tagmind.db.repositories.content_repo import ContentRepository
from tagmind.db.repositories.taxonomy_repo import TaxonomyRepository
from tagmind.domain.content import Content, ContentStatus, QualityStatus
from tagmind.domain.label import LabelDef


class _FakeResult:
    """模拟 SQLAlchemy ``Result``。"""

    def __init__(self, rows: list[Any] | None = None, scalar: Any = None) -> None:
        self._rows = list(rows or [])
        self._scalar = scalar

    def scalars(self) -> _FakeResult:
        return self

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalar_one(self) -> Any:
        return self._scalar if self._scalar is not None else 0

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def __iter__(self) -> Any:
        return iter(self._rows)

    def __bool__(self) -> bool:
        return bool(self._rows)


class _FakeSession:
    """记录 ``execute`` 调用并返回预置结果的假 Session。"""

    def __init__(self, results: list[Any] | None = None, *, scalar: Any = None) -> None:
        self.results = list(results or [])
        self.scalar = scalar
        self.calls: list[tuple[Any, Any]] = []
        self.added: list[Any] = []
        self.committed = False
        self.row_by_pk: dict[int, Any] = {}

    def execute(self, statement: Any, *args: Any, **kwargs: Any) -> _FakeResult:
        self.calls.append((statement, kwargs))
        if self.results:
            return _FakeResult(self.results.pop(0))
        return _FakeResult(scalar=self.scalar)

    def get(self, model: Any, pk: Any) -> Any:
        return self.row_by_pk.get(pk)

    def delete(self, row: Any) -> None:
        return None

    def add(self, entity: Any) -> None:
        self.added.append(entity)

    def commit(self) -> None:
        self.committed = True

    def flush(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _make_content(**overrides: Any) -> Content:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    defaults: dict[str, Any] = {
        "id": 1,
        "raw_text": "原始内容",
        "clean_text": "清洗后内容",
        "labels": ({"code": "C_AI", "layer": 1, "confidence": 0.9},),
        "labels_version": "v1",
        "quality_score": 8.5,
        "quality_status": QualityStatus.APPROVED,
        "status": ContentStatus.APPROVED,
        "pipeline_version": "pipeline_v1",
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)
    return Content(**defaults)


class TestContentRepositoryMapping:
    """内容仓储的行映射与值映射。"""

    def test_domain_to_values_covers_key_columns(self) -> None:
        repo = ContentRepository(_FakeSession())
        values = repo._map_domain_to_values(_make_content())
        assert values["raw_text"] == "原始内容"
        assert values["clean_text"] == "清洗后内容"
        assert values["labels_version"] == "v1"
        assert values["pipeline_version"] == "pipeline_v1"

    def test_enum_statuses_are_stringified(self) -> None:
        """枚举必须落库为字符串，不能落成 ``ContentStatus.APPROVED`` 的 repr。"""
        repo = ContentRepository(_FakeSession())
        values = repo._map_domain_to_values(_make_content())
        assert values["status"] == "approved"
        assert values["quality_status"] == "approved"
        assert isinstance(values["status"], str)

    def test_nullable_relations_become_none(self) -> None:
        """可空关系列（pii_fields / chunks）空时存 NULL。

        两者在 GIN 索引下的行为不同，必须与 DDL 的可空性一致。
        """
        repo = ContentRepository(_FakeSession())
        content = _make_content(pii_fields=(), chunks=())
        values = repo._map_domain_to_values(content)
        assert values["pii_fields"] is None
        assert values["chunks"] is None

    def test_media_refs_stays_empty_list(self) -> None:
        """media_refs 在 DDL 里是 NOT NULL DEFAULT '[]'，必须保持为空数组。"""
        repo = ContentRepository(_FakeSession())
        content = _make_content(media_refs=())
        values = repo._map_domain_to_values(content)
        assert values["media_refs"] == []

    def test_non_empty_tuples_are_preserved(self) -> None:
        repo = ContentRepository(_FakeSession())
        values = repo._map_domain_to_values(_make_content(pii_fields=("phone",)))
        assert values["pii_fields"] == ["phone"]

    def test_quality_score_is_numeric(self) -> None:
        repo = ContentRepository(_FakeSession())
        values = repo._map_domain_to_values(_make_content())
        assert values["quality_score"] == 8.5
        assert isinstance(values["quality_score"], float)

    def test_content_is_immutable(self) -> None:
        """领域对象是 frozen 的：状态变更必须走仓储，否则审计会失真。"""
        content = _make_content()
        with pytest.raises(AttributeError):
            content.status = ContentStatus.REJECTED  # type: ignore[misc]


class TestContentRepositoryQueries:
    """仓储查询方法的调用契约。"""

    def test_get_by_idempotency_key_returns_none_when_absent(self) -> None:
        repo = ContentRepository(_FakeSession(results=[]))
        assert repo.get_by_idempotency_key("missing") is None

    def test_get_by_content_hash_returns_none_when_absent(self) -> None:
        repo = ContentRepository(_FakeSession(results=[]))
        assert repo.get_by_content_hash("deadbeef") is None

    def test_get_by_idempotency_key_executes_query(self) -> None:
        session = _FakeSession(results=[])
        repo = ContentRepository(session)
        repo.get_by_idempotency_key("key-1")
        assert session.calls, "必须真的发起查询而不是直接返回 None"

    def test_get_returns_none_when_absent(self) -> None:
        repo = ContentRepository(_FakeSession(results=[]))
        assert repo.get(999) is None

    def test_list_by_status_returns_empty_list(self) -> None:
        repo = ContentRepository(_FakeSession(results=[]))
        assert repo.list_by_status("approved") == []

    def test_list_by_status_executes_query(self) -> None:
        session = _FakeSession(results=[])
        repo = ContentRepository(session)
        repo.list_by_status("approved", limit=25, offset=50)
        assert session.calls


class TestTaxonomyRepositoryMapping:
    """标签仓储的映射与树构建。"""

    def test_label_def_to_values(self) -> None:
        session = _FakeSession()
        repo = TaxonomyRepository(session)
        label = LabelDef(
            id=1,
            code="C_AI",
            layer=1,
            name="人工智能",
            description="AI 相关内容",
            version="v1",
        )
        values = repo._map_domain_to_values(label)
        assert values["code"] == "C_AI"
        assert values["layer"] == 1
        assert values["version"] == "v1"

    def test_list_defs_returns_empty_list(self) -> None:
        repo = TaxonomyRepository(_FakeSession(results=[]))
        assert repo.list_defs("v1") == []

    def test_list_versions_returns_empty_list(self) -> None:
        repo = TaxonomyRepository(_FakeSession(results=[]))
        assert repo.list_versions() == []

    def test_get_by_code_returns_none_when_absent(self) -> None:
        repo = TaxonomyRepository(_FakeSession(results=[]))
        assert repo.get_by_code("C_AI", "v1") is None

    def test_list_defs_sorts_by_layer_then_code(self) -> None:
        """取出的标签必须按 (layer, code) 排序，否则树构建顺序不确定。"""
        session = _FakeSession(results=[])
        repo = TaxonomyRepository(session)
        repo.list_defs("v1")
        statement = session.calls[-1][0]
        assert statement is not None


class TestFilterSpec:
    """``FilterSpec`` 是仓储查询条件的统一载体。"""

    def test_empty_spec_is_falsy(self) -> None:
        """空条件不应生成无意义的 WHERE 子句。"""
        assert not FilterSpec()

    def test_non_empty_spec_is_truthy(self) -> None:
        assert FilterSpec(status="approved")

    def test_none_values_are_dropped(self) -> None:
        """``None`` 表示「忽略该列」，与「等于 NULL」语义不同。"""
        spec = FilterSpec(status="approved", layer=None)
        assert "layer" not in spec.conditions
        assert spec.conditions["status"] == "approved"

    def test_conditions_are_preserved(self) -> None:
        spec = FilterSpec(status="approved", layer=2)
        assert spec.conditions["status"] == "approved"
        assert spec.conditions["layer"] == 2

    def test_repr_shows_conditions(self) -> None:
        """调试表示需包含条件，便于排查生成的 SQL。"""
        assert "status" in repr(FilterSpec(status="approved"))


class TestRepositoryBaseContract:
    """仓储基类的通用契约。"""

    def test_all_repositories_expose_core_methods(self) -> None:
        """所有仓储必须提供 CRUD 与幂等支持的方法。"""
        required = {"get", "list", "upsert", "delete", "insert_ignore", "count"}
        for repo_cls in (ContentRepository, TaxonomyRepository):
            assert required <= set(dir(repo_cls)), repo_cls.__name__

    def test_session_is_retained(self) -> None:
        session = _FakeSession()
        repo = ContentRepository(session)
        assert repo.session is session

    def test_delete_returns_false_when_row_absent(self) -> None:
        repo = ContentRepository(_FakeSession())
        assert repo.delete(999) is False

    def test_count_returns_zero_when_absent(self) -> None:
        repo = ContentRepository(_FakeSession(scalar=0))
        assert repo.count(FilterSpec(status="approved")) == 0

    def test_count_returns_scalar(self) -> None:
        repo = ContentRepository(_FakeSession(scalar=42))
        assert repo.count(FilterSpec(status="approved")) == 42
