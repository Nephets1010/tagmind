"""数据库元数据防线测试。

这个模块锁定「16 张物理表」这一契约，防止后续阶段新增模块时静默漂移：

* ORM 侧 ``Base.metadata.tables`` 必须恰好是约定的 16 张表，无多无少；
* 每个 ``Base`` 子类的 ``__tablename__`` 必须全局唯一；
* 迁移脚本（原生 SQL DDL）创建的表集合必须与 ORM 完全一致；
* ``exposure_log`` 必须保持分区表形态。

这些断言是 ADR-0009（幂等以数据库唯一约束为最终保证）的配套防线：
表结构是幂等与一致性校验的地基，漂移会让所有下游保证失效。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# 导入模型包以触发 ORM 注册；否则 metadata 为空，断言会失去意义。
import tagmind.db.models as _models  # noqa: F401
from tagmind.db.base import Base

# 架构文档 §5 约定的 16 张物理表。
# `exposure_log_default` 是分区表的 DEFAULT 分区，属实现细节，不计入契约。
EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "content",
        "label_def",
        "label_conflict_log",
        "user_profile",
        "exposure_log",
        "content_vector",
        "llm_call_audit",
        "egress_log",
        "review_task",
        "review_record",
        "experiment",
        "experiment_assignment",
        "annotation_set",
        "annotation",
        "label_drift_check",
        "pii_mask_map",
    }
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations" / "versions"

_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?\"?([a-z_][a-z0-9_]*)\"?",
    re.IGNORECASE,
)

#: SQL 关键字白名单：正则可能把 `IF` 之类的保留字误当作表名。
_SQL_KEYWORDS: frozenset[str] = frozenset({"IF", "NOT", "EXISTS", "TABLE", "ONLY"})


def _collect_subclasses(cls: type) -> set[type]:
    """递归收集 ``cls`` 的全部子类。"""
    found: set[type] = set()
    for sub in cls.__subclasses__():
        found.add(sub)
        found |= _collect_subclasses(sub)
    return found


def _mapped_table_names() -> list[str]:
    """收集所有已映射的 ``__tablename__``。"""
    names: list[str] = []
    for sub in _collect_subclasses(Base):
        name = getattr(sub, "__tablename__", None)
        if isinstance(name, str):
            names.append(name)
    return names


def _migration_table_names() -> set[str]:
    """从迁移脚本的原生 SQL 中提取建表名。"""
    names: set[str] = set()
    for path in sorted(MIGRATIONS_DIR.glob("*.py")):
        for raw in _CREATE_TABLE_RE.findall(path.read_text(encoding="utf-8")):
            if raw.upper() in _SQL_KEYWORDS:
                continue
            names.add(raw.lower())
    # DEFAULT 分区不是契约表的一部分。
    names.discard("exposure_log_default")
    return names


class TestOrmMetadataContract:
    """ORM 元数据必须与 16 张表的契约严格一致。"""

    def test_metadata_tables_exactly_match_contract(self) -> None:
        """ORM 侧表集合恰好等于约定的 16 张，无多无少。"""
        assert set(Base.metadata.tables) == set(EXPECTED_TABLES)

    def test_table_count_is_sixteen(self) -> None:
        """显式锁死表数量，避免误改断言导致契约松脱。"""
        assert len(Base.metadata.tables) == 16

    def test_tablenames_are_globally_unique(self) -> None:
        """每个 Base 子类的 __tablename__ 全局唯一，防止重复注册。"""
        names = _mapped_table_names()
        duplicates = sorted({name for name in names if names.count(name) > 1})
        assert not duplicates, f"重复注册的表名：{duplicates}"

    def test_user_profile_mapped_exactly_once(self) -> None:
        """user_profile 曾因重复导入被注册两次，此断言锁死回归。"""
        names = _mapped_table_names()
        assert names.count("user_profile") == 1

    def test_every_mapped_class_belongs_to_contract(self) -> None:
        """所有已映射的表都在契约集合内，不存在游离表。"""
        assert set(_mapped_table_names()) <= set(EXPECTED_TABLES)


class TestMigrationMatchesOrm:
    """迁移脚本与 ORM 模型必须描述同一套表。"""

    def test_migration_tables_equal_orm_tables(self) -> None:
        """迁移建表集合与 ORM 表集合完全相等。"""
        assert _migration_table_names() == set(EXPECTED_TABLES)

    def test_exposure_log_is_partitioned(self) -> None:
        """exposure_log 必须保持分区表形态（Q10 按月分区）。

        分区声明在迁移脚本的原生 SQL 里（``PARTITION BY RANGE (ts)``），
        ORM 侧不重复声明，因此以迁移脚本为分区事实来源。
        """
        ddl = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS_DIR.glob("*.py"))
        )
        assert re.search(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?exposure_log\b",
            ddl,
            re.IGNORECASE,
        ), "迁移必须创建 exposure_log"
        assert re.search(
            r"PARTITION\s+BY\s+RANGE\s*\(\s*ts\s*\)",
            ddl,
            re.IGNORECASE,
        ), "exposure_log 必须按 ts 做 RANGE 分区"
        assert "exposure_log_default" in ddl, "必须存在 DEFAULT 分区兜底"

    def test_no_migration_creates_unknown_table(self) -> None:
        """迁移不得创建契约外的表。"""
        unknown = _migration_table_names() - set(EXPECTED_TABLES)
        assert not unknown, f"迁移创建了契约外的表：{sorted(unknown)}"


class TestOrmImportsAreSideEffectFree:
    """重复导入模型模块不得引发重复注册。"""

    def test_reimporting_models_is_idempotent(self) -> None:
        """再次 import 模型包不改变表集合。"""
        import importlib

        import tagmind.db.models as models

        before = set(Base.metadata.tables)
        importlib.reload(models)
        after = set(Base.metadata.tables)
        assert before == after
        assert len(after) == 16


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_table_has_primary_key(table_name: str) -> None:
    """每张表都必须有主键，否则幂等与追溯都无从谈起。"""
    table = Base.metadata.tables[table_name]
    assert table.primary_key.columns, f"{table_name} 缺少主键"
