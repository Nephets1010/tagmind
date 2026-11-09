"""仓储层集成测试（需要 PostgreSQL）。

对应架构 §14.5 的仓储约定：

- 一个仓储方法 = 一个事务
- 唯一约束冲突 → 视为幂等重放，**不重试**直接返回既有行
- 乐观锁：条件更新未命中即视为版本冲突
- 仓储对外返回 **domain 对象**，不泄漏 ORM 细节

无 DB 时整个模块 skip。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from tagmind.db.repositories.base import FilterSpec, retry_on_deadlock
from tagmind.db.repositories.content_repo import ContentRepository
from tagmind.db.repositories.interaction_repo import InteractionRepository
from tagmind.db.repositories.taxonomy_repo import TaxonomyRepository
from tagmind.db.repositories.workflow_repo import WorkflowRepository
from tagmind.db.session import build_engine
from tagmind.domain.content import Content, ContentStatus, QualityStatus
from tagmind.domain.interaction import BehaviorAction, ExposureEvent
from tagmind.domain.label import ConflictType

pytestmark = pytest.mark.integration

#: 每个测试前清空的业务表（保证测试之间互不干扰）。
_BUSINESS_TABLES = (
    "content, exposure_log, label_conflict_log, review_record, review_task, "
    "user_profile, content_vector, pii_mask_map, llm_call_audit, egress_log, "
    "annotation, annotation_set, experiment, experiment_assignment, label_drift_check"
)


@pytest.fixture(scope="module")
def test_engine(migrated_db: str):
    """指向测试库的 Engine。

    刻意**不用**全局 ``get_engine()``：那个是 ``lru_cache`` 单例，按
    ``Settings`` 里的 ``TAGMIND_POSTGRES_*`` 解析——测试机上那多半指向
    开发库，用它会污染真实数据。
    """
    engine = build_engine(url=migrated_db)
    yield engine
    engine.dispose()


@pytest.fixture
def test_session(test_engine):
    """测试库上的 Session（自带提交 / 回滚边界）。"""
    factory = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@pytest.fixture(autouse=True)
def clean_tables(test_session: Session) -> None:
    """每个测试前清空业务表。

    架构 §14.8 的「测试数据隔离」要求：不隔离的话，第二次跑就会撞上
    第一次留下的 ``idempotency_key``，报 UniqueViolation——测试失败的原因
    与被测代码无关。
    """
    test_session.execute(text(f"TRUNCATE {_BUSINESS_TABLES} RESTART IDENTITY CASCADE"))
    test_session.commit()


@pytest.fixture
def content_repo(test_session: Session) -> ContentRepository:
    """内容仓储。"""
    return ContentRepository(test_session)


@pytest.fixture
def interaction_repo(test_session: Session) -> InteractionRepository:
    """交互仓储。"""
    return InteractionRepository(test_session)


@pytest.fixture
def workflow_repo(test_session: Session) -> WorkflowRepository:
    """工作流仓储。"""
    return WorkflowRepository(test_session)


def _make_content(**overrides) -> Content:
    """构造一个测试内容。"""
    base = {
        "id": 0,
        "raw_text": "原始正文",
        "clean_text": "清洗后正文",
        "labels_version": "ld_v1",
        "pipeline_version": "test|ld_v1|pt|p|ct|ts",
        "idempotency_key": "idem-test-001",
    }
    base.update(overrides)
    return Content(**base)


def test_upsert_and_get_roundtrip(content_repo: ContentRepository) -> None:
    """写入后能按主键读回，且返回的是 domain 对象。"""
    saved = content_repo.upsert(_make_content())
    assert saved.id > 0
    fetched = content_repo.get(saved.id)
    assert fetched is not None
    assert isinstance(fetched, Content)
    assert fetched.clean_text == "清洗后正文"
    assert fetched.labels_version == "ld_v1"


def test_idempotency_key_lookup(content_repo: ContentRepository) -> None:
    """可按幂等键查询内容。"""
    saved = content_repo.upsert(_make_content(idempotency_key="idem-lookup-1"))
    found = content_repo.get_by_idempotency_key("idem-lookup-1")
    assert found is not None
    assert found.id == saved.id


def test_insert_ignore_returns_existing_on_conflict(content_repo: ContentRepository) -> None:
    """唯一约束冲突时返回既有行，且 ``created=False``（幂等重放）。"""
    first, created_first = content_repo.insert_ignore(
        _make_content(idempotency_key="idem-ignore-1")
    )
    assert created_first is True
    second, created_second = content_repo.insert_ignore(
        _make_content(idempotency_key="idem-ignore-1")
    )
    assert created_second is False, "重复插入不应报告为新建"
    assert second.id == first.id


def test_upsert_labels_is_atomic(content_repo: ContentRepository) -> None:
    """标签与版本号在同一次写入中落库（C10 一致性）。"""
    saved = content_repo.upsert(_make_content(idempotency_key="idem-labels-1"))
    updated = content_repo.upsert_labels(
        saved.id,
        labels=[{"label_id": 1, "code": "D_TECH", "layer": 1, "confidence": 0.9}],
        label_raw={"low_confidence": []},
        label_stats={"per_layer": {"1": 1}},
        labels_version="ld_v2",
        pipeline_version="test|ld_v2|pt|p|ct|ts",
        label_ids=[1],
        layer_label_ids=[1],
        l1_code="D_TECH",
        l2_code="C_AI",
    )
    assert updated.labels_version == "ld_v2"
    assert updated.label_ids == (1,)
    assert updated.l1_code == "D_TECH"
    assert updated.label_raw is not None


def test_search_by_labels(content_repo: ContentRepository) -> None:
    """标签倒排检索能命中。

    显式传 ``status=""`` —— 内容此时还是 ``pending``，而仓储的默认过滤是
    ``approved``（只检索可分发的内容）。
    """
    saved = content_repo.upsert(_make_content(idempotency_key="idem-search-1"))
    content_repo.upsert_labels(
        saved.id,
        labels=[{"label_id": 4242, "code": "T_TEST", "layer": 4, "confidence": 0.8}],
        label_raw=None,
        label_stats=None,
        labels_version="ld_v1",
        pipeline_version="pv",
        label_ids=[4242],
        layer_label_ids=[4242],
    )
    hits = content_repo.search_by_labels([4242], status="")
    assert any(hit.id == saved.id for hit in hits)


def test_search_by_labels_filters_by_status(content_repo: ContentRepository) -> None:
    """默认只检索 ``approved`` 内容（分发索引口径）。"""
    saved = content_repo.upsert(_make_content(idempotency_key="idem-search-2"))
    content_repo.upsert_labels(
        saved.id,
        labels=[{"label_id": 4343, "code": "T_TEST2", "layer": 4, "confidence": 0.8}],
        label_raw=None,
        label_stats=None,
        labels_version="ld_v1",
        pipeline_version="pv",
        label_ids=[4343],
        layer_label_ids=[4343],
    )
    assert content_repo.search_by_labels([4343]) == []


def test_mark_status(content_repo: ContentRepository) -> None:
    """状态更新落库。"""
    saved = content_repo.upsert(_make_content(idempotency_key="idem-status-1"))
    now = datetime.now(UTC)
    updated = content_repo.mark_status(
        saved.id,
        ContentStatus.APPROVED,
        quality_status=QualityStatus.APPROVED,
        quality_score=8.5,
        published_at=now,
    )
    assert updated.status == ContentStatus.APPROVED
    assert updated.quality_score == 8.5
    assert updated.published_at is not None


def test_optimistic_update_detects_conflict(
    content_repo: ContentRepository, test_session: Session
) -> None:
    """乐观锁：版本不匹配时条件更新未命中。

    期望版本必须是调用方**先前读到**的值——这就是乐观锁的语义：
    「我基于我看到的那个版本做修改」。若别人已改过，条件更新就失配。
    """
    saved = content_repo.upsert(_make_content(idempotency_key="idem-lock-1"))
    stale_version = saved.updated_at

    # 别人改了一次（触发器随之刷新 updated_at）。
    test_session.execute(
        text("UPDATE content SET quality_reason = '外部改动' WHERE id = :cid"),
        {"cid": saved.id},
    )
    result = content_repo.optimistic_update(
        saved.id,
        {"quality_reason": "手动修改"},
        expected_version_column="updated_at",
        expected_version=stale_version,
    )
    assert result is False, "版本过期时条件更新应当未命中"


def test_optimistic_update_succeeds_with_current_version(
    content_repo: ContentRepository,
) -> None:
    """版本一致时条件更新成功。"""
    saved = content_repo.upsert(_make_content(idempotency_key="idem-lock-2"))
    result = content_repo.optimistic_update(
        saved.id,
        {"quality_reason": "无并发改动"},
        expected_version_column="updated_at",
        expected_version=saved.updated_at,
    )
    assert result is True


def test_bulk_insert_exposures_is_idempotent(
    interaction_repo: InteractionRepository, test_session: Session
) -> None:
    """曝光批量写入幂等（``ON CONFLICT DO NOTHING``）。"""
    repo = ContentRepository(test_session)
    saved = repo.upsert(_make_content(idempotency_key="idem-exposure-1"))
    content_id = saved.id

    ts = datetime.now(UTC)
    events = [
        ExposureEvent(
            user_id="user-1",
            content_id=content_id,
            action=BehaviorAction.CLICK,
            ts=ts,
            request_id="req-1",
            rank_pos=1,
            algo="recall_only",
            recall_source="hot",
        )
    ]
    first = interaction_repo.bulk_insert_exposures(events)
    second = interaction_repo.bulk_insert_exposures(events)
    assert first == 1
    assert second == 0, "重复写入应被幂等约束拦下"


def test_hot_items_uses_effective_interactions(
    interaction_repo: InteractionRepository, test_session: Session
) -> None:
    """热门榜只统计有效交互（click/dwell），view 不计入。"""
    repo = ContentRepository(test_session)
    a = repo.upsert(_make_content(idempotency_key="idem-hot-a"))
    b = repo.upsert(_make_content(idempotency_key="idem-hot-b"))
    content_a, content_b = a.id, b.id

    ts = datetime.now(UTC)
    events = [
        ExposureEvent(
            user_id="u-hot",
            content_id=content_a,
            action=BehaviorAction.VIEW,
            ts=ts,
            request_id="req-hot-1",
        ),
        ExposureEvent(
            user_id="u-hot",
            content_id=content_b,
            action=BehaviorAction.CLICK,
            ts=ts,
            request_id="req-hot-2",
        ),
    ]
    interaction_repo.bulk_insert_exposures(events)
    hot = interaction_repo.hot_items(ts - timedelta(hours=1), ts + timedelta(hours=1))
    counts = dict(hot)
    assert counts.get(content_b, 0) == 1
    assert content_a not in counts, "view 不应计入热门榜"


def test_effective_interaction_count(
    interaction_repo: InteractionRepository, test_session: Session
) -> None:
    """有效交互数按 A-09 口径统计。"""
    repo = ContentRepository(test_session)
    saved = repo.upsert(_make_content(idempotency_key="idem-eff-1"))
    content_id = saved.id

    ts = datetime.now(UTC)
    events = [
        ExposureEvent(
            user_id="u-eff",
            content_id=content_id,
            action=action,
            ts=ts,
            request_id=f"req-eff-{idx}",
        )
        for idx, action in enumerate(
            [BehaviorAction.CLICK, BehaviorAction.DWELL, BehaviorAction.VIEW, BehaviorAction.SKIP]
        )
    ]
    interaction_repo.bulk_insert_exposures(events)
    assert interaction_repo.effective_interaction_count("u-eff") == 2
    # behavior_count：view 计 0.5，click/dwell 计 1，skip 计 0
    assert interaction_repo.behavior_count_for("u-eff") == 2.5


def test_workflow_insert_conflict_and_review_task(
    workflow_repo: WorkflowRepository, test_session: Session
) -> None:
    """冲突日志写入 + 复核任务幂等创建 + 轨迹追加。"""
    repo = ContentRepository(test_session)
    saved = repo.upsert(_make_content(idempotency_key="idem-workflow-1"))
    content_id = saved.id

    log = workflow_repo.insert_conflict(
        content_id=content_id,
        conflict_type=ConflictType.MUTUAL,
        detected_by="rule",
        candidates=[
            {"label_id": 1, "code": "C_A", "layer": 2, "confidence": 0.9},
            {"label_id": 2, "code": "C_B", "layer": 2, "confidence": 0.8},
        ],
        rule_hits=[{"rule_id": "C2-001", "detail": "同父互斥"}],
    )
    assert log.id > 0
    assert log.conflict_type == ConflictType.MUTUAL
    assert len(log.candidates) == 2

    task = workflow_repo.create_review_task(
        task_type="conflict", target_type="conflict", target_id=log.id
    )
    assert task.id > 0
    # 同一目标重复创建 → 唯一约束拦下，返回既有任务
    again = workflow_repo.create_review_task(
        task_type="conflict", target_type="conflict", target_id=log.id
    )
    assert again.id == task.id

    record = workflow_repo.append_review_record(
        task_id=task.id, reviewer="tester", action="approve", content_id=content_id
    )
    assert record.id > 0
    assert len(workflow_repo.list_review_records(task.id)) == 1


def test_taxonomy_repo_load_tree(test_session: Session) -> None:
    """标签仓储能装载标签树（依赖已导入的种子数据）。"""
    repo = TaxonomyRepository(test_session)
    versions = repo.list_versions()
    if not versions:
        pytest.skip("尚未导入标签体系种子")
    tree = repo.load_tree(versions[0])
    assert tree.total() > 0
    assert tree.roots


def test_retry_on_deadlock_passes_through() -> None:
    """``retry_on_deadlock`` 对可重试错误会重试并最终成功。"""
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _FakeOperationalError()
        return "ok"

    assert retry_on_deadlock(flaky, max_attempts=3, base_delay_s=0.001) == "ok"
    assert calls["n"] == 3


def test_retry_on_deadlock_gives_up() -> None:
    """重试次数耗尽后抛出 :class:`DatabaseError`。"""
    from tagmind.core.errors import DatabaseError

    def always_fail() -> None:
        raise _FakeOperationalError()

    with pytest.raises(DatabaseError):
        retry_on_deadlock(always_fail, max_attempts=2, base_delay_s=0.001)


def test_filterspec_ignores_none() -> None:
    """``FilterSpec`` 忽略值为 ``None`` 的条件。"""
    spec = FilterSpec(status="approved", user_id=None)
    assert spec.conditions == {"status": "approved"}
    assert bool(spec) is True
    assert not FilterSpec()


def test_domain_objects_not_leaked(content_repo: ContentRepository) -> None:
    """仓储返回 domain 对象，不返回 ORM 行。"""
    from tagmind.db.models.content import Content as ContentModel

    saved = content_repo.upsert(_make_content(idempotency_key="idem-domain-1"))
    fetched = content_repo.get(saved.id)
    assert not isinstance(fetched, ContentModel)
    assert isinstance(fetched, Content)


class _FakeOrig:
    """模拟原始 DBAPI 错误对象（携带 psycopg 的 ``sqlstate``）。"""

    sqlstate = "40P01"
    pgcode = "40P01"


class _FakeOperationalError(OperationalError):
    """模拟 ``OperationalError``（带可重试 sqlstate）。"""

    def __init__(self) -> None:
        super().__init__("UPDATE 1", {}, _FakeOrig())
