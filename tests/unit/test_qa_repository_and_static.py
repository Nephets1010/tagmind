"""QA 补充测试：仓储基类与标签树的未覆盖语义（阶段 0）。

变异测试暴露出 ``db/repositories/base.py`` 的 4 条路径（``optimistic_update``
返回值、``insert_ignore`` 的 inserted 标志、``_drop_server_managed``、
``retry_on_deadlock``）以及 ``labeling/taxonomy.py`` 的 ``validate_release``
A2 校验完全没有被单测覆盖——把实现改坏，既有的 274 个单元测试仍全绿。

本文件用轻量假 Session 覆盖这些路径，全部为纯逻辑，不依赖 PostgreSQL。

同时以「静态契约」形式锁定 CI / 容器配置中容易静默漂移的项：
迁移链、compose 服务、healthcheck 路径一致性、以及 ``celery include``
指向的模块必须真实存在（这是阶段 0 独立验证发现的 Blocker）。
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from tagmind.core.errors import DatabaseError
from tagmind.db.base import Base
from tagmind.db.repositories.base import FilterSpec, retry_on_deadlock
from tagmind.db.repositories.content_repo import ContentRepository
from tagmind.labeling.taxonomy import parse_taxonomy

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class _FakeResult:
    """带可控 rowcount / first / all 的假 Result。"""

    def __init__(self, rowcount: int = 0, rows: list[Any] | None = None) -> None:
        self.rowcount = rowcount
        self._rows = rows or []

    def scalars(self) -> _FakeResult:
        return self

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalar_one(self) -> Any:
        return self._rows[0] if self._rows else 0


class _FakeSavepoint:
    """记录 rollback / commit 是否被调用。"""

    def __init__(self) -> None:
        self.rolled_back = False
        self.committed = False

    def rollback(self) -> None:
        self.rolled_back = True

    def commit(self) -> None:
        self.committed = True


class _FakeSession:
    """可控的假 Session：覆盖 flush 抛错、rowcount、嵌套事务、版本查询。"""

    def __init__(
        self,
        *,
        rowcount: int = 1,
        flush_error: Exception | None = None,
        version_row: tuple[Any, ...] | None = ("2026-01-01T00:00:00Z",),
    ) -> None:
        self.rowcount = rowcount
        self.flush_error = flush_error
        self.version_row = version_row
        self.calls: list[Any] = []
        self.added: list[Any] = []
        self.savepoint: _FakeSavepoint | None = None
        self.row_by_pk: dict[int, Any] = {}
        self.flushed = 0

    def execute(self, statement: Any, *args: Any, **kwargs: Any) -> _FakeResult:
        self.calls.append(statement)
        rows = [self.version_row] if self.version_row is not None else []
        return _FakeResult(self.rowcount, rows)

    def get(self, model: Any, pk: Any) -> Any:
        return self.row_by_pk.get(pk)

    def add(self, entity: Any) -> None:
        self.added.append(entity)

    def flush(self) -> None:
        self.flushed += 1
        if self.flush_error is not None:
            raise self.flush_error

    def begin_nested(self) -> _FakeSavepoint:
        self.savepoint = _FakeSavepoint()
        return self.savepoint

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


class _FakeOrig:
    """模拟 psycopg 原始错误对象。"""

    sqlstate = "40P01"


class _RetryableOperationalError(OperationalError):
    """可重试的死锁错误。"""

    def __init__(self) -> None:
        super().__init__("UPDATE 1", {}, _FakeOrig())


class _NonRetryableOperationalError(OperationalError):
    """不可重试的 OperationalError（sqlstate 不在 40001 / 40P01）。"""

    def __init__(self) -> None:
        class _Orig:
            sqlstate = "08006"  # connection_failure

        super().__init__("connection lost", {}, _Orig())


class TestOptimisticUpdate:
    """乐观锁的返回值语义（变异测试存活点之一）。"""

    def test_returns_true_when_row_was_updated(self) -> None:
        """rowcount=1 → True。"""
        session = _FakeSession(rowcount=1)
        repo = ContentRepository(session)  # type: ignore[arg-type]
        assert repo.optimistic_update(1, {"status": "approved"}) is True

    def test_returns_false_on_version_conflict(self) -> None:
        """rowcount=0 → False；这是「别人改过这行」的唯一信号。"""
        session = _FakeSession(rowcount=0)
        repo = ContentRepository(session)  # type: ignore[arg-type]
        assert repo.optimistic_update(1, {"status": "approved"}) is False

    def test_explicit_expected_version_is_used(self) -> None:
        """显式传入 expected_version 时不应再读库，直接用它做 WHERE 条件。"""
        session = _FakeSession(rowcount=1, version_row=None)
        repo = ContentRepository(session)  # type: ignore[arg-type]
        assert repo.optimistic_update(1, {"status": "approved"}, expected_version="v-old") is True
        assert len(session.calls) == 1, "显式版本下不应额外发起版本查询"

    def test_raises_when_row_missing(self) -> None:
        """版本查询无行时抛 ``OptimisticLockError``，不静默返回 False。"""
        from tagmind.core.errors import OptimisticLockError

        repo = ContentRepository(_FakeSession(version_row=None))  # type: ignore[arg-type]
        with pytest.raises(OptimisticLockError):
            repo.optimistic_update(999, {"status": "approved"})


class TestInsertIgnore:
    """幂等插入的 ``inserted`` 标志与 SAVEPOINT 语义。"""

    def test_new_row_reports_inserted_true(self) -> None:
        """无冲突时返回 (实体, True) 并提交 savepoint。"""
        session = _FakeSession()
        repo = ContentRepository(session)  # type: ignore[arg-type]
        entity = _make_content(idempotency_key="qa-new")
        result, inserted = repo.insert_ignore(entity)
        assert inserted is True
        assert result.idempotency_key == "qa-new"
        assert session.savepoint is not None and session.savepoint.committed

    def test_conflict_reports_inserted_false_and_rolls_back(self) -> None:
        """唯一约束冲突时返回 (既有行, False) 并回滚 savepoint。"""
        from tagmind.domain.content import Content

        existing = _make_content(idempotency_key="qa-dup")
        session = _FakeSession(flush_error=IntegrityError("INSERT", {}, Exception("dup")))
        repo = ContentRepository(session)  # type: ignore[arg-type]
        repo._find_by_unique = lambda values: existing  # type: ignore[method-assign]
        result, inserted = repo.insert_ignore(existing)
        assert inserted is False, "冲突时必须回报 False，否则幂等重放会被当成新写入"
        assert isinstance(result, Content)
        assert session.savepoint is not None and session.savepoint.rolled_back

    def test_conflict_without_findable_row_reraises(self) -> None:
        """冲突但查不到既有行时必须原样抛出，不能吞掉完整性错误。"""
        session = _FakeSession(flush_error=IntegrityError("INSERT", {}, Exception("dup")))
        repo = ContentRepository(session)  # type: ignore[arg-type]
        repo._find_by_unique = lambda values: None  # type: ignore[method-assign]
        with pytest.raises(IntegrityError):
            repo.insert_ignore(_make_content(idempotency_key="qa-orphan"))


class TestServerManagedColumns:
    """``_drop_server_managed`` 不得覆盖数据库维护的列。"""

    def test_none_on_server_default_column_is_dropped(self) -> None:
        """created_at=None 必须剔除，让 now() 生效。"""
        repo = ContentRepository(_FakeSession())  # type: ignore[arg-type]
        out = repo._drop_server_managed({"created_at": None, "raw_text": "x"})
        assert "created_at" not in out
        assert out["raw_text"] == "x"

    def test_none_on_plain_nullable_column_is_kept(self) -> None:
        """published_at 没有 server_default，业务上写 NULL 必须保留。"""
        repo = ContentRepository(_FakeSession())  # type: ignore[arg-type]
        out = repo._drop_server_managed({"published_at": None})
        assert "published_at" in out


class TestRetryOnDeadlock:
    """死锁重试的分支覆盖（变异测试存活点）。"""

    def test_retries_retryable_error_then_succeeds(self) -> None:
        """40P01 重试到成功，且确实调用了 3 次。"""
        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise _RetryableOperationalError()
            return "ok"

        assert retry_on_deadlock(flaky, max_attempts=3, base_delay_s=0.001) == "ok"
        assert calls["n"] == 3

    def test_exhaustion_raises_database_error(self) -> None:
        """重试耗尽抛 DatabaseError，而不是把 OperationalError 直接漏出去。"""

        def always_fail() -> None:
            raise _RetryableOperationalError()

        with pytest.raises(DatabaseError):
            retry_on_deadlock(always_fail, max_attempts=2, base_delay_s=0.001)

    def test_non_retryable_error_is_not_retried(self) -> None:
        """连接失效等非并发争用错误只调用一次，不重试。"""
        calls = {"n": 0}

        def fail() -> None:
            calls["n"] += 1
            raise _NonRetryableOperationalError()

        with pytest.raises(OperationalError):
            retry_on_deadlock(fail, max_attempts=3, base_delay_s=0.001)
        assert calls["n"] == 1


class TestTaxonomyValidateRelease:
    """``validate_release`` 的 A2 校验必须真的能拦住错误的父子层关系。"""

    def test_wrong_parent_layer_is_reported(self) -> None:
        """L3 节点的 parent 是 L1 → 必须产生 A2 违规。

        这是变异测试存活点：把 ``_validate_structure`` 的层检查改成
        ``elif False``，既有 274 个单测仍全绿。
        """
        tree = parse_taxonomy(
            {
                1: [{"code": "A", "name": "甲"}],
                3: [{"code": "B", "name": "乙", "parent": "A"}],
            }
        )
        violations = tree.validate_release()
        assert any("A2" in v and "父节点" in v for v in violations), violations

    def test_l1_with_parent_is_reported(self) -> None:
        """L1 节点带 parent 必须产生 A1 违规。"""
        tree = parse_taxonomy(
            {
                1: [{"code": "A", "name": "甲"}],
                2: [{"code": "B", "name": "乙", "parent": "A"}],
            }
        )
        # 手工构造一个违规节点来验证 A1 分支
        from dataclasses import replace

        from tagmind.labeling.taxonomy import TaxonomyTree

        bad = replace(tree.by_code["A"], parent="B")
        broken = TaxonomyTree(
            version=tree.version,
            nodes=(bad, *[n for n in tree.nodes if n.code != "A"]),
            by_code=tree.by_code,
            children=tree.children,
            roots=tree.roots,
            layer_nodes=tree.layer_nodes,
        )
        violations = broken.validate_release()
        assert any("A1" in v for v in violations), violations

    def test_wrong_path_is_reported(self) -> None:
        """手工篡改 path 必须产生 A4 违规（物化路径一致性不可被绕过）。"""
        from dataclasses import replace

        from tagmind.labeling.taxonomy import TaxonomyTree

        tree = parse_taxonomy(
            {
                1: [{"code": "A", "name": "甲"}],
                2: [{"code": "B", "name": "乙", "parent": "A"}],
            }
        )
        bad = replace(tree.by_code["B"], path="WRONG/PATH")
        broken = TaxonomyTree(
            version=tree.version,
            nodes=(tree.by_code["A"], bad),
            by_code=tree.by_code,
            children=tree.children,
            roots=tree.roots,
            layer_nodes=tree.layer_nodes,
        )
        violations = broken.validate_release()
        assert any("A4" in v for v in violations), violations


class TestStaticInfrastructureContracts:
    """无法真跑容器/PG，至少把静态契约钉死，防止静默漂移。"""

    def test_migration_down_revision_chain_is_linear(self) -> None:
        """5 个迁移形成单一线性链：恰好 1 个 head、1 个 base，无环无分叉。"""
        versions = REPO_ROOT / "migrations" / "versions"
        chain: dict[str, str | None] = {}
        for path in sorted(versions.glob("[0-9]*.py")):
            text = path.read_text(encoding="utf-8")
            rev = re.search(r'^revision:\s*str\s*=\s*"([^"]+)"', text, re.MULTILINE)
            down = re.search(r"^down_revision:\s*str\s*\|\s*None\s*=\s*(.+$)", text, re.MULTILINE)
            assert rev, f"{path.name} 缺少 revision"
            name = rev.group(1)
            parent = None if down is None else down.group(1).strip().strip('"')
            if parent == "None":
                parent = None
            chain[name] = parent
        assert len(chain) == 5, f"预期 5 个迁移，实际 {len(chain)}"
        bases = [k for k, v in chain.items() if v is None]
        assert len(bases) == 1, f"base 数量异常：{bases}"
        children = [v for v in chain.values() if v is not None]
        assert len(set(children)) == len(children), "down_revision 出现分叉（多 head）"
        # 从 base 走 parent 链必须恰好覆盖全部 5 个 revision
        seen: set[str] = set()
        cursor: str | None = bases[0]
        while cursor is not None:
            assert cursor in chain, f"down_revision 指向不存在的 {cursor}"
            assert cursor not in seen, f"迁移链存在环：{cursor}"
            seen.add(cursor)
            cursor = next((k for k, v in chain.items() if v == cursor), None)
        assert seen == set(chain), f"链未覆盖全部 revision：{set(chain) - seen}"

    def test_compose_defines_all_five_services(self) -> None:
        """compose 必须含 api / worker / beat / postgres / redis 五个服务。"""
        text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        for service in ("api", "worker", "beat", "postgres", "redis"):
            assert re.search(rf"^\s{{2}}{service}:\s*$", text, re.MULTILINE), f"缺少服务 {service}"

    def test_healthcheck_paths_match_between_compose_and_app(self) -> None:
        """compose 与 Dockerfile 的健康路径必须与 create_app 注册的路径一致。"""
        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        app = (REPO_ROOT / "src" / "tagmind" / "api" / "app.py").read_text(encoding="utf-8")
        assert "/healthz" in compose
        assert "/healthz" in dockerfile
        assert re.search(r'@app\.get\("/healthz"', app)

    def test_no_hardcoded_credential_literals_in_compose(self) -> None:
        """compose 不得出现明文密码字面量；密码必须走 ${...} 变量。

        注意：``${VAR:-tagmind}`` 这种本地开发默认值是可以接受的（不是
        生产密钥），但仍需保证 ``TAGMIND_POSTGRES_PASSWORD`` 本身从环境注入。
        """
        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        assert "TAGMIND_POSTGRES_PASSWORD" in compose
        # 形如 PASSWORD: "明文" 的硬编码（不含 ${ }）
        hardcoded = re.findall(r"POSTGRES_PASSWORD:\s*['\"]([^'\"]+)['\"]", compose)
        assert not hardcoded, f"compose 硬编码了密码：{hardcoded}"

    def test_celery_include_target_exists(self) -> None:
        """``celery_app.include`` 指向的模块必须真实存在，否则 worker/beat 启动即崩。

        这是 QA 独立验证发现的 Blocker（原 ``tagmind.scheduler.tasks`` 缺失，
        ``celery worker`` / ``beat`` 在 ``init_worker()`` 抛
        ``ModuleNotFoundError``）；工程师已补上该包，本用例锁死回归。
        """
        from tagmind.scheduler.celery_app import celery_app

        includes = list(celery_app.conf.include or [])
        assert includes, "include 为空"
        for module in includes:
            try:
                importlib.import_module(module)
            except ModuleNotFoundError as exc:
                pytest.fail(
                    f"celery include 指向不存在的模块 {module!r}：worker/beat 无法启动（{exc}）"
                )

    def test_orm_metadata_has_exactly_sixteen_tables(self) -> None:
        """16 张表契约：从不同角度再锁一次，防止 test_db_metadata 被空集绕过。"""
        import tagmind.db.models as _models  # noqa: F401

        assert len(Base.metadata.tables) == 16
        assert "exposure_log" in Base.metadata.tables
        assert "pii_mask_map" in Base.metadata.tables

    def test_filterspec_drops_none(self) -> None:
        """FilterSpec 的 None 语义（此用例原本被 autouse DB fixture 误 skip）。"""
        spec = FilterSpec(status="approved", layer=None)
        assert spec.conditions == {"status": "approved"}
        assert bool(spec)


def _make_content(**overrides: Any) -> Any:
    """构造最小 Content 领域对象。"""
    from tagmind.domain.content import Content

    defaults: dict[str, Any] = {
        "id": 0,
        "raw_text": "正文",
        "clean_text": "正文",
        "labels_version": "ld_v1",
        "pipeline_version": "test|ld_v1|pt|p|ct|ts",
    }
    defaults.update(overrides)
    return Content(**defaults)
