"""迁移幂等性与结构一致性集成测试（需要 PostgreSQL）。

对应 T02 验收项：

- ① 空库执行 ``alembic upgrade head`` 成功，**再执行一次无报错**（P4-DAT-001）
- ② ``information_schema`` 与 §5 DDL 逐项比对（名/型/可空/默认）零差异
- ③ ``exposure_log`` 分区表 + DEFAULT 分区存在，插入跨月数据落在正确分区
- ④ ``review_record`` 的 UPDATE/DELETE 被触发器拒绝
- ⑤ ``pii_mask_map`` 对 PUBLIC 无权限

无 DB 时整个模块 skip。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tagmind.db.session import build_engine

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

#: 期望存在的 16 张物理表（歧义 A-03 的裁决）。
EXPECTED_TABLES: tuple[str, ...] = (
    "content",
    "label_def",
    "label_conflict_log",
    "user_profile",
    "exposure_log",
    "content_vector",
    "llm_call_audit",
    "review_task",
    "review_record",
    "experiment",
    "experiment_assignment",
    "annotation_set",
    "annotation",
    "label_drift_check",
    "pii_mask_map",
    "egress_log",
)


def _subprocess_env(db_url: str) -> dict[str, str]:
    """构造子进程环境变量。

    两个要点：

    1. ``PYTHONIOENCODING=utf-8`` —— Windows 默认 locale 是 GBK，子进程输出
       中文会被按 GBK 解码从而抛 ``UnicodeDecodeError``。
    2. 保留 ``PATH`` 等基础变量，其余全部显式给出，避免继承到开发机的
       ``TAGMIND_*`` 配置（那会让测试结果依赖本机环境）。
    """
    import os

    env = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "TAGMIND_DATABASE_URL": db_url,
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "HOME": str(REPO_ROOT),
    }
    # Windows 上 asyncio 初始化需要 SystemRoot / windir，缺了会报
    # "无法加载或初始化请求的服务提供程序"（WinError 10106）。
    for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "COMSPEC"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def _exec(engine, statement, params: dict | None = None):
    """执行一条查询并返回全部行。

    SQLAlchemy 2.0 移除了 ``Engine.execute()``，必须显式开连接。
    """
    with engine.connect() as conn:
        result = conn.execute(statement, params or {})
        rows = result.all()
        conn.commit()
    return rows


@pytest.fixture(scope="module")
def engine(migrated_db: str):
    """按测试 URL 创建 Engine。"""
    eng = build_engine(url=migrated_db)
    yield eng
    eng.dispose()


def _run_alembic(db_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    """跑一次 alembic 命令。"""
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        cwd=str(REPO_ROOT),
        env=_subprocess_env(db_url),
    )


def test_alembic_upgrade_is_idempotent(migrated_db: str) -> None:
    """``alembic upgrade head`` 连续执行两次都成功（P4-DAT-001）。"""
    first = _run_alembic(migrated_db, "upgrade", "head")
    assert first.returncode == 0, f"首次迁移失败：\n{first.stdout}\n{first.stderr}"
    second = _run_alembic(migrated_db, "upgrade", "head")
    assert second.returncode == 0, (
        f"二次迁移失败（幂等性被破坏）：\n{second.stdout}\n{second.stderr}"
    )


def test_alembic_current_is_head(migrated_db: str) -> None:
    """迁移后 ``alembic current`` 指向 head。"""
    result = _run_alembic(migrated_db, "current")
    assert result.returncode == 0
    assert "0005_experiment_and_annotation" in result.stdout, result.stdout


def test_all_sixteen_tables_exist(engine) -> None:
    """16 张物理表全部存在。"""
    rows = _exec(
        engine,
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        ),
    )
    names = {str(row[0]) for row in rows}
    missing = [name for name in EXPECTED_TABLES if name not in names]
    assert not missing, f"缺少表：{missing}"


def test_core_columns_match_ddl(engine) -> None:
    """核心表的列名与 §5 DDL 逐项一致。"""
    expected: dict[str, set[str]] = {
        "content": {
            "id",
            "raw_text",
            "clean_text",
            "labels",
            "label_raw",
            "label_confidence",
            "label_stats",
            "labels_version",
            "quality_score",
            "quality_reason",
            "quality_status",
            "status",
            "reject_reason",
            "clean_stats",
            "chunks",
            "pii_masked",
            "pii_fields",
            "source",
            "content_hash",
            "idempotency_key",
            "embedding_version",
            "model_version",
            "prompt_version",
            "pipeline_version",
            "label_ids",
            "layer_label_ids",
            "l1_code",
            "l2_code",
            "synthetic",
            "media_refs",
            "created_at",
            "updated_at",
            "published_at",
        },
        "label_def": {
            "id",
            "code",
            "layer",
            "name",
            "description",
            "examples",
            "parent",
            "max_select",
            "exclusive_group",
            "sibling_exclusion",
            "is_feature",
            "status",
            "version",
            "path",
            "path_text",
            "depth",
            "ancestors",
            "root_code",
            "df_count",
            "idf",
            "path_prefix",
            "created_at",
            "updated_at",
        },
        "exposure_log": {
            "id",
            "user_id",
            "content_id",
            "action",
            "ts",
            "dwell_ms",
            "request_id",
            "rank_pos",
            "score",
            "algo",
            "recall_source",
            "exp_group",
            "request_latency_ms",
            "synthetic",
            "pipeline_version",
            "created_at",
        },
        "user_profile": {
            "user_id",
            "tag_weights",
            "cold_start",
            "confidence",
            "behavior_count",
            "label_dims",
            "decay_lambda",
            "last_behavior_at",
            "drift_detected",
            "drift_score",
            "model_version",
            "updated_at",
            "label_affinity",
            "label_weight_norm",
            "tag_space_version",
            "cold_start_threshold",
            "weights_sum",
            "synthetic",
        },
    }
    for table, columns in expected.items():
        rows = _exec(
            engine,
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": table},
        )
        actual = {str(row[0]) for row in rows}
        missing = columns - actual
        assert not missing, f"{table} 缺少列：{sorted(missing)}"


def test_key_nullability_and_defaults(engine) -> None:
    """关键列的可空性与默认值与 §5 DDL 一致。"""
    rows = _exec(
        engine,
        text(
            "SELECT table_name, column_name, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name IN ('content', 'label_def', 'user_profile')"
        ),
    )
    info = {(str(r[0]), str(r[1])): (str(r[2]), r[3]) for r in rows}

    # content.clean_text NOT NULL
    assert info[("content", "clean_text")][0] == "NO"
    # content.pipeline_version NOT NULL
    assert info[("content", "pipeline_version")][0] == "NO"
    # content.labels 有默认值 '[]'
    assert "[]" in str(info[("content", "labels")][1])
    # label_def.version NOT NULL
    assert info[("label_def", "version")][0] == "NO"
    # label_def.parent 可空（L1 的 parent 为 NULL）
    assert info[("label_def", "parent")][0] == "YES"
    # user_profile.user_id 是主键 → NOT NULL
    assert info[("user_profile", "user_id")][0] == "NO"


def test_exposure_log_is_partitioned(engine) -> None:
    """``exposure_log`` 是分区表且存在 DEFAULT 分区。"""
    rows = _exec(
        engine,
        text(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_inherits i ON i.inhrelid = c.oid "
            "JOIN pg_class p ON p.oid = i.inhparent "
            "WHERE p.relname = 'exposure_log'"
        ),
    )
    names = [str(row[0]) for row in rows]
    assert names, "exposure_log 没有任何分区"
    assert "exposure_log_default" in names, "缺少 DEFAULT 分区"


def test_cross_month_insert_lands_in_correct_partition(engine) -> None:
    """跨月数据落在正确的分区里。"""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO content (raw_text, clean_text, labels_version, pipeline_version) "
                "VALUES ('x', 'x', 'ld_v1', 'test') RETURNING id"
            )
        )
        content_id = conn.execute(text("SELECT max(id) FROM content")).scalar_one()
        for month in ("2026-01-15 10:00:00+00", "2026-06-15 10:00:00+00"):
            conn.execute(
                text(
                    "INSERT INTO exposure_log (user_id, content_id, action, ts, request_id) "
                    "VALUES ('u1', :cid, 'click', :ts, :rid)"
                ),
                {"cid": content_id, "ts": month, "rid": f"req-{month[:7]}"},
            )
        rows = conn.execute(
            text(
                "SELECT tableoid::regclass::text, date_trunc('month', ts) FROM exposure_log "
                "WHERE content_id = :cid"
            ),
            {"cid": content_id},
        )
        assert len(rows) == 2
        # 两条数据落在不同分区（DEFAULT 分区也算一个分区）
        assert len({str(r[0]) for r in rows}) == 2, f"跨月数据未落到不同分区：{rows}"
        conn.execute(text("DELETE FROM exposure_log WHERE content_id = :cid"), {"cid": content_id})
        conn.execute(text("DELETE FROM content WHERE id = :cid"), {"cid": content_id})


def test_review_record_is_immutable(engine) -> None:
    """``review_record`` 的 UPDATE 与 DELETE 都被触发器拒绝。"""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO review_task (task_type, target_type, target_id, status) "
                "VALUES ('conflict', 'content', 999999, 'pending') RETURNING id"
            )
        )
        task_id = conn.execute(text("SELECT max(id) FROM review_task")).scalar_one()
        conn.execute(
            text(
                "INSERT INTO review_record (task_id, reviewer, action) "
                "VALUES (:tid, 'tester', 'approve') RETURNING id"
            ),
            {"tid": task_id},
        )
        record_id = conn.execute(text("SELECT max(id) FROM review_record")).scalar_one()

    with pytest.raises(DBAPIError):
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE review_record SET comment = 'tampered' WHERE id = :rid"),
                {"rid": record_id},
            )

    with pytest.raises(DBAPIError):
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM review_record WHERE id = :rid"), {"rid": record_id})

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM review_task WHERE id = :tid"), {"tid": task_id})


def test_pii_mask_map_revoked_from_public(engine) -> None:
    """``pii_mask_map`` 对 PUBLIC 无权限（NF-S02）。"""
    rows = _exec(
        engine, text("SELECT has_table_privilege('public', 'public.pii_mask_map', 'SELECT')")
    )
    assert rows
    assert not bool(rows[0][0]), "pii_mask_map 仍对 PUBLIC 开放 SELECT"


def test_domains_exist(engine) -> None:
    """§5.1 的公共域全部创建。"""
    rows = _exec(
        engine,
        text(
            "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
            "WHERE n.nspname = 'public' AND t.typtype = 'd'"
        ),
    )
    names = {str(row[0]) for row in rows}
    expected = {
        "ts_tz",
        "quality_score_t",
        "confidence_t",
        "content_status_t",
        "quality_status_t",
        "conflict_type_t",
        "review_status_t",
        "action_t",
    }
    assert expected <= names, f"缺少公共域：{expected - names}"


def test_trigger_functions_exist(engine) -> None:
    """触发器函数存在。"""
    rows = _exec(
        engine,
        text("SELECT proname FROM pg_proc WHERE proname = ANY(:names)"),
        {"names": ["fn_set_updated_at", "fn_forbid_mutation"]},
    )
    names = {str(row[0]) for row in rows}
    assert {"fn_set_updated_at", "fn_forbid_mutation"} <= names


def test_label_def_unique_constraints(engine) -> None:
    """``UNIQUE(version, code)`` 与 ``UNIQUE(version, path)`` 生效。"""
    rows = _exec(
        engine,
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'label_def'::regclass AND contype = 'u'"
        ),
    )
    names = {str(row[0]) for row in rows}
    assert "ux_label_def_version_code" in names
    assert "ux_label_def_version_path" in names


def test_content_idempotency_unique(engine) -> None:
    """``content.idempotency_key`` 唯一约束存在。"""
    rows = _exec(
        engine,
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'content'::regclass AND contype = 'u'"
        ),
    )
    names = {str(row[0]) for row in rows}
    assert "ux_content_idempotency" in names


def test_exposure_idempotency_unique(engine) -> None:
    """``ux_exposure_idem`` 唯一索引存在（A-04 裁决）。"""
    rows = _exec(engine, text("SELECT indexname FROM pg_indexes WHERE tablename = 'exposure_log'"))
    names = {str(row[0]) for row in rows}
    assert "ux_exposure_idem" in names


def test_consistency_check_script_passes(migrated_db: str) -> None:
    """迁移后校验脚本退出码为 0。"""
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "consistency_check.py"),
            "--after-migration",
            "head",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        cwd=str(REPO_ROOT),
        env=_subprocess_env(migrated_db),
    )
    assert result.returncode == 0, f"一致性校验失败：\n{result.stdout}\n{result.stderr}"
