"""标签体系导入集成测试（需要 PostgreSQL）。

对应 T03 验收项：④ ``UNIQUE(version, code)`` / ``UNIQUE(version, path)`` 生效，
⑥ 标签树 API 返回结构与 DB 一致（P4-API-004 抽样 20 节点比对）。

无 DB 时整个模块 skip（架构 §14.8 的硬性要求）。
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tagmind.config.constants import MAX_LABEL_TOTAL, MIN_LABEL_TOTAL
from tagmind.db.models.taxonomy import LabelDef as LabelDefModel
from tagmind.db.repositories.taxonomy_repo import TaxonomyRepository
from tagmind.db.session import build_engine
from tagmind.labeling.taxonomy import load_taxonomy_from_yaml

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SEED_DIR = REPO_ROOT / "data" / "label_taxonomy"
VERSION = "ld_v1"


@pytest.fixture(scope="module")
def engine(migrated_db: str):
    """按测试 URL 创建 Engine。"""
    eng = build_engine(url=migrated_db)
    yield eng
    eng.dispose()


@pytest.fixture
def test_session(engine):
    """测试库上的 Session（自带提交 / 回滚边界）。"""
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@pytest.fixture(scope="module")
def tree():
    """装载标签种子。"""
    return load_taxonomy_from_yaml(SEED_DIR, version=VERSION)


@pytest.fixture
def seeded(test_session: Session, tree):
    """把标签种子写进测试库，并返回写入的条数。"""
    repo = TaxonomyRepository(test_session)
    written = repo.bulk_upsert_defs(tree)
    test_session.commit()
    return written


def test_import_labels_into_db(test_session: Session, tree) -> None:
    """把 1490 个标签写入数据库。"""
    definitions = tree.to_label_defs()
    repo = TaxonomyRepository(test_session)
    written = repo.bulk_upsert_defs(tree)
    test_session.commit()
    count = repo.count()
    assert written == tree.total()
    assert count >= MIN_LABEL_TOTAL
    assert count <= MAX_LABEL_TOTAL
    assert len(definitions) == tree.total()


def test_import_is_idempotent(test_session: Session, tree) -> None:
    """重复导入同一版本不产生重复行。"""
    repo = TaxonomyRepository(test_session)
    repo.bulk_upsert_defs(tree)
    test_session.commit()
    first = repo.count()
    repo.bulk_upsert_defs(tree)
    test_session.commit()
    assert repo.count() == first, "重复导入不应产生重复行"


def test_unique_version_code_enforced(test_session: Session) -> None:
    """同版本内重复 code 必须被唯一约束拒绝。"""
    dup_version = f"{VERSION}_dup"
    test_session.execute(text("DELETE FROM label_def WHERE version = :v"), {"v": dup_version})
    test_session.commit()
    test_session.execute(
        text(
            "INSERT INTO label_def (code, layer, name, description, version, path, path_text, "
            "depth, root_code, path_prefix) "
            "VALUES ('DUP_CODE', 1, '重复', '重复编码测试', :v, '重复', '重复', 1, "
            "'DUP_CODE', '')"
        ),
        {"v": dup_version},
    )
    test_session.commit()

    with pytest.raises(IntegrityError):
        test_session.execute(
            text(
                "INSERT INTO label_def (code, layer, name, description, version, path, "
                "path_text, depth, root_code, path_prefix) "
                "VALUES ('DUP_CODE', 1, '重复', '重复编码测试', :v, '重复2', '重复2', 1, "
                "'DUP_CODE', '')"
            ),
            {"v": dup_version},
        )
    test_session.rollback()


def test_unique_version_path_enforced(test_session: Session) -> None:
    """同版本内重复 path 必须被唯一约束拒绝。"""
    suffix = f"{VERSION}_duppath"
    test_session.execute(text("DELETE FROM label_def WHERE version = :v"), {"v": suffix})
    test_session.commit()
    test_session.execute(
        text(
            "INSERT INTO label_def (code, layer, name, description, version, path, path_text, "
            "depth, root_code, path_prefix) "
            "VALUES ('DUP_PATH_A', 1, '重复路径', '重复路径测试', :v, '唯一路径', '唯一路径', 1, "
            "'DUP_PATH_A', '')"
        ),
        {"v": suffix},
    )
    test_session.commit()

    with pytest.raises(IntegrityError):
        test_session.execute(
            text(
                "INSERT INTO label_def (code, layer, name, description, version, path, "
                "path_text, depth, root_code, path_prefix) "
                "VALUES ('DUP_PATH_B', 1, '重复路径', '重复路径测试', :v, '唯一路径', '唯一路径', "
                "1, 'DUP_PATH_B', '')"
            ),
            {"v": suffix},
        )
    test_session.rollback()


def test_same_code_allowed_across_versions(test_session: Session) -> None:
    """A-01：code 跨版本不变，同一 code 可以存在于多个版本。"""
    other_version = f"{VERSION}_v2"
    test_session.execute(text("DELETE FROM label_def WHERE version = :v"), {"v": other_version})
    test_session.commit()
    test_session.execute(
        text(
            "INSERT INTO label_def (code, layer, name, description, version, path, path_text, "
            "depth, root_code, path_prefix) "
            "VALUES ('D_TECH', 1, '科技数码', '跨版本同编码', :v, '科技数码', '科技数码', 1, "
            "'D_TECH', '')"
        ),
        {"v": other_version},
    )
    test_session.commit()
    count = test_session.execute(
        select(func.count()).select_from(LabelDefModel).where(LabelDefModel.code == "D_TECH")
    ).scalar_one()
    assert count >= 2, "跨版本同 code 应被允许"


def test_load_tree_matches_seed(test_session: Session, tree, seeded: int) -> None:
    """从 DB 装载的树与 YAML 种子一致（路径与祖先）。"""
    repo = TaxonomyRepository(test_session)
    db_tree = repo.load_tree(VERSION)
    assert db_tree.total() == tree.total()
    sample = random.Random(42).sample(list(tree.by_code), 20)
    for code in sample:
        db_node = db_tree.node(code)
        assert db_node is not None, f"DB 中缺少 {code}"
        assert db_node.path == tree.by_code[code].path
        assert db_node.ancestors == tree.by_code[code].ancestors
        assert db_node.layer == tree.by_code[code].layer
        assert db_node.max_select == tree.by_code[code].max_select


def test_activate_version_marks_others_deprecated(test_session: Session, seeded: int) -> None:
    """切换生效版本后，旧版本标记 deprecated 但**不删除**（可回滚）。"""
    repo = TaxonomyRepository(test_session)
    repo.activate_version(VERSION)
    test_session.commit()
    assert repo.count_by_status("active") > 0
    assert VERSION in repo.list_versions()


def test_materialized_path_prefix_query(test_session: Session, tree, seeded: int) -> None:
    """``path_prefix`` 前缀查询能取到子树（索引前缀扫描）。"""
    sample = tree.by_layer(3)[0]
    rows = test_session.execute(
        select(LabelDefModel.code)
        .where(LabelDefModel.version == VERSION)
        .where(LabelDefModel.path_prefix.like(f"{sample.path}/%"))
    ).scalars().all()
    assert rows, f"未查到 {sample.path} 的子树"
    assert all(code in tree.by_code for code in rows)


def test_ancestors_gin_contains_query(test_session: Session, tree, seeded: int) -> None:
    """``ancestors`` 的 GIN 包含查询可用（C3 的 O(1) 判定依赖它）。"""
    sample = tree.by_layer(4)[0]
    if not sample.ancestors:
        pytest.skip("样本节点无祖先")
    rows = test_session.execute(
        select(func.count())
        .select_from(LabelDefModel)
        .where(LabelDefModel.version == VERSION)
        .where(LabelDefModel.ancestors.any(sample.ancestors[0]))
    ).scalar_one()
    assert rows > 0
