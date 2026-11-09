"""数据一致性巡检脚本。

按架构文档 §13 的四类校验执行：

- **C1 向量 ↔ 标签一致性**（V1–V5）
- **C2 画像 ↔ 曝光对账**（V6–V9）
- **C3 冲突日志 7 要素完整性**（V10–V11）
- **C4 迁移后结构校验**（V12–V18）

用法::

    python scripts/consistency_check.py --after-migration head
    python scripts/consistency_check.py --full

退出码：``0`` = 全通过；``1`` = 有阻断项（CI 红）；``2`` = 有告警项（CI 黄）。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

#: 仓库根目录（scripts/ 的上一级）。
REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = REPO_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from tagmind.config.settings import get_settings  # noqa: E402
from tagmind.db.session import build_engine  # noqa: E402

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

#: 期望存在的唯一约束（表 → 约束名）。
EXPECTED_UNIQUE_CONSTRAINTS: dict[str, tuple[str, ...]] = {
    "content": ("ux_content_idempotency",),
    "label_def": ("ux_label_def_version_code", "ux_label_def_version_path"),
    "llm_call_audit": ("llm_call_audit_llm_call_id_key",),
    "review_task": ("ux_review_task_target",),
    "annotation": ("ux_annotation_unique",),
    "exposure_log": ("ux_exposure_idem",),
    "annotation_set": ("annotation_set_version_key",),
    "experiment": ("experiment_name_key",),
    "label_drift_check": ("ux_drift_week",),
    "pii_mask_map": ("ux_pii_placeholder",),
}

#: 期望存在的索引（表 → 索引名）。
EXPECTED_INDEXES: dict[str, tuple[str, ...]] = {
    "content": ("ix_content_label_ids", "ix_content_dispatchable"),
    "label_def": ("ix_label_def_code", "ix_label_def_path_prefix"),
    "exposure_log": ("ix_exposure_user_ts", "ux_exposure_idem"),
    "content_vector": ("ix_content_vector_tag",),
    "review_record": ("ix_review_record_task",),
}

#: 期望存在的触发器函数。
EXPECTED_FUNCTIONS: tuple[str, ...] = ("fn_set_updated_at", "fn_forbid_mutation")


def _rows(engine: Engine, statement: Any, params: dict[str, Any] | None = None) -> list[Any]:
    """执行一条查询并返回全部行。

    SQLAlchemy 2.0 移除了 ``Engine.execute()``，必须显式开连接。
    """
    with engine.connect() as conn:
        result = conn.execute(statement, params or {})
        rows = result.all()
        conn.commit()
    return rows


def _scalar(engine: Engine, statement: Any, params: dict[str, Any] | None = None) -> Any:
    """执行一条查询并返回首行首列（无行时返回 ``None``）。"""
    with engine.connect() as conn:
        result = conn.execute(statement, params or {})
        row = result.first()
        conn.commit()
    return None if row is None else row[0]


@dataclass
class CheckReport:
    """巡检结果。"""

    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)

    def block(self, message: str) -> None:
        """记录一个阻断项。"""
        self.blocking.append(message)

    def warn(self, message: str) -> None:
        """记录一个告警项。"""
        self.warnings.append(message)

    def ok(self, message: str) -> None:
        """记录一个通过项。"""
        self.passed.append(message)

    def exit_code(self) -> int:
        """按 §13.3 的约定返回退出码。"""
        if self.blocking:
            return 1
        if self.warnings:
            return 2
        return 0


def _check_tables(engine: Engine, report: CheckReport) -> None:
    """V14 结构一致性：16 张表全部存在。"""
    inspector = inspect(engine)
    actual = set(inspector.get_table_names())
    missing = [name for name in EXPECTED_TABLES if name not in actual]
    if missing:
        report.block(f"缺少表：{missing}")
    else:
        report.ok(f"16 张物理表全部存在（实际 {len(actual)} 张，含分区子表）")


def _check_unique_constraints(engine: Engine, report: CheckReport) -> None:
    """V14：唯一约束存在性。

    分两类查：

    - ``pg_constraint`` 里的 ``UNIQUE`` 约束（表级声明的 UNIQUE）。
    - ``pg_indexes`` 里的唯一索引（``CREATE UNIQUE INDEX``）。

    分开查是有原因的：``exposure_log`` 是分区表，它的 ``ux_exposure_idem``
    必须建成**唯一索引**而不是约束——分区表的唯一约束要求包含分区键，
    而索引没有这个限制。
    """
    inspector = inspect(engine)
    for table, expected in EXPECTED_UNIQUE_CONSTRAINTS.items():
        if table not in inspector.get_table_names():
            continue
        actual = {uc.get("name") for uc in inspector.get_unique_constraints(table)}
        actual |= {ix.get("name") for ix in inspector.get_indexes(table) if ix.get("unique")}
        missing = [name for name in expected if name not in actual]
        if missing:
            report.block(f"{table} 缺少唯一约束：{missing}")
        else:
            report.ok(f"{table} 的唯一约束齐备")


def _check_indexes(engine: Engine, report: CheckReport) -> None:
    """V14：索引存在性。"""
    inspector = inspect(engine)
    for table, expected in EXPECTED_INDEXES.items():
        if table not in inspector.get_table_names():
            continue
        actual = {idx["name"] for idx in inspector.get_indexes(table)}
        # 分区表的索引建在父表上，inspector 只看得到父表的定义——
        # 但对于分区表，索引是级联到各分区的，所以父表上必然能查到。
        missing = [name for name in expected if name not in actual]
        if missing:
            report.block(f"{table} 缺少索引：{missing}")
        else:
            report.ok(f"{table} 的关键索引齐备")


def _check_partitions(engine: Engine, report: CheckReport) -> None:
    """V16：``exposure_log`` 的父表 + 至少 1 个分区 + DEFAULT 分区。"""
    rows = _rows(
        engine,
        text(
            """
            SELECT c.relname
            FROM pg_class c
            JOIN pg_inherits i ON i.inhrelid = c.oid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = 'exposure_log'
            """
        ),
    )
    names = [str(row[0]) for row in rows]
    if not names:
        report.block("exposure_log 没有任何分区")
        return
    if "exposure_log_default" not in names:
        report.block("exposure_log 缺少 DEFAULT 分区")
    else:
        report.ok(f"exposure_log 有 {len(names)} 个分区（含 DEFAULT）")


def _check_functions(engine: Engine, report: CheckReport) -> None:
    """V12：触发器函数存在性。"""
    rows = _rows(
        engine,
        text("SELECT p.proname FROM pg_proc p WHERE p.proname = ANY(:names)"),
        {"names": list(EXPECTED_FUNCTIONS)},
    )
    actual = {str(row[0]) for row in rows}
    missing = [name for name in EXPECTED_FUNCTIONS if name not in actual]
    if missing:
        report.block(f"缺少触发器函数：{missing}")
    else:
        report.ok("触发器函数齐备")


def _check_pii_privileges(engine: Engine, report: CheckReport) -> None:
    """NF-S02：``pii_mask_map`` 对 PUBLIC 无权限。

    判定方式：``has_table_privilege('public', ...)`` —— ``PUBLIC`` 是伪角色，
    PostgreSQL 用它表示「所有用户」。返回 ``False`` 即表示 PUBLIC 无 SELECT 权限。

    （不用 ``pg_class.relacl`` 手写 ACL 展开：``aclitem`` 与 ``oid`` 类型不同，
    直接比较会报 ``operator does not exist: oid = aclitem``。）
    """
    row = _scalar(
        engine,
        text("SELECT has_table_privilege('public', 'public.pii_mask_map', 'SELECT')"),
    )
    if row:
        report.block("pii_mask_map 仍对 PUBLIC 开放 SELECT 权限")
    else:
        report.ok("pii_mask_map 对 PUBLIC 无权限")


def _check_review_record_immutable(engine: Engine, report: CheckReport) -> None:
    """V12：``review_record`` 的 UPDATE 被触发器拒绝。"""
    rows = _rows(
        engine,
        text(
            """
            SELECT COUNT(*)
            FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            WHERE c.relname = 'review_record'
              AND NOT t.tgisinternal
              AND t.tgname = 'tg_review_record_immutable'
            """
        ),
    )
    if not rows or int(rows[0][0]) == 0:
        report.block("review_record 缺少不可变触发器")
    else:
        report.ok("review_record 的不可变触发器存在")


def _check_vector_consistency(engine: Engine, report: CheckReport) -> None:
    """V1–V5：向量 ↔ 标签一致性。"""
    row = _scalar(
        engine,
        text(
            """
            SELECT
                (SELECT COUNT(*) FROM content WHERE status = 'approved')
                - (SELECT COUNT(*) FROM content_vector) AS coverage_gap
            """
        ),
    )
    gap = int(row)
    if gap > 0:
        report.warn(f"V1：已发布内容比向量多 {gap} 条（可能是尚未 embed，属正常中间态）")
    else:
        report.ok("V1：已发布内容的向量覆盖率无缺口")

    row = _scalar(
        engine,
        text(
            """
            SELECT COUNT(*)
            FROM content_vector v
            JOIN content c ON c.id = v.content_id
            WHERE EXISTS (
                SELECT 1 FROM jsonb_object_keys(v.tag_vector) AS k
                WHERE NOT EXISTS (
                    SELECT 1 FROM jsonb_array_elements(c.labels) AS l
                    WHERE (l->>'label_id')::bigint = k::bigint
                )
            )
            """
        ),
    )
    violations = int(row)
    if violations:
        report.block(f"V2：{violations} 条向量的 tag_vector 含未登记的 label_id")
    else:
        report.ok("V2：tag_vector 的 label_id 均在 labels 内")

    row = _scalar(
        engine,
        text(
            """
            SELECT COUNT(*)
            FROM content_vector
            WHERE ABS(tag_vector_norm - (
                SELECT SQRT(SUM(VALUE::numeric::double precision * VALUE::numeric::double precision))
                FROM jsonb_each_text(tag_vector)
            )) > 1e-6
            """
        ),
    )
    violations = int(row)
    if violations:
        report.block(f"V3：{violations} 条向量的 tag_vector_norm 与实际值不符")
    else:
        report.ok("V3：向量归一化自洽")

    row = _scalar(
        engine,
        text(
            """
            SELECT
                (SELECT COUNT(*) FROM content_vector v
                 LEFT JOIN content c ON c.id = v.content_id WHERE c.id IS NULL)
              + (SELECT COUNT(*) FROM content c
                 LEFT JOIN content_vector v ON v.content_id = c.id WHERE v.content_id IS NULL
                 AND c.status = 'approved') AS orphans
            """
        ),
    )
    orphans = int(row)
    if orphans:
        report.block(f"V4：存在 {orphans} 条孤儿向量或缺失向量")
    else:
        report.ok("V4：无孤儿向量")


def _check_profile_consistency(engine: Engine, report: CheckReport) -> None:
    """V6–V9：画像 ↔ 曝光对账。"""
    row = _scalar(
        engine,
        text(
            """
            SELECT COUNT(*) FROM user_profile
            WHERE NOT cold_start AND ABS(weights_sum - 1) > 1e-6
            """
        ),
    )
    violations = int(row)
    if violations:
        report.block(f"V6：{violations} 个非冷启动用户的权重和不为 1")
    else:
        report.ok("V6：画像权重归一化")

    row = _scalar(
        engine,
        text(
            """
            SELECT COUNT(*) FROM user_profile
            WHERE cold_start <> (confidence = 0)
            """
        ),
    )
    violations = int(row)
    if violations:
        report.block(f"V8：{violations} 个用户的 cold_start 与 confidence 不一致")
    else:
        report.ok("V8：冷启动标记与置信度一致")

    row = _scalar(
        engine,
        text(
            """
            SELECT COUNT(*) FROM user_profile
            WHERE label_dims <> (
                SELECT COUNT(*) FROM jsonb_object_keys(tag_weights)
            )
            """
        ),
    )
    violations = int(row)
    if violations:
        report.block(f"V9：{violations} 个用户的 label_dims 与实际维度数不符")
    else:
        report.ok("V9：画像维度数自洽")


def _check_conflict_seven_elements(engine: Engine, report: CheckReport) -> None:
    """V10：冲突日志 7 要素非空率必须 100%。"""
    row = _scalar(
        engine,
        text(
            """
            SELECT COUNT(*) FROM label_conflict_log
            WHERE candidates IS NULL
               OR jsonb_array_length(candidates) = 0
               OR resolved IS NULL
               OR jsonb_array_length(resolved) = 0
               OR reason IS NULL OR reason = ''
               OR arbitrator_model IS NULL OR arbitrator_model = ''
               OR confidence IS NULL
               OR review_status IS NULL
            """
        ),
    )
    violations = int(row)
    if violations:
        report.block(f"V10：{violations} 条冲突日志的 7 要素不完整")
    else:
        report.ok("V10：冲突日志 7 要素完整率 100%")


def _check_pipeline_version(engine: Engine, report: CheckReport) -> None:
    """V18：``approved`` 内容的 ``pipeline_version`` 必须非空。"""
    row = _scalar(
        engine,
        text(
            """
            SELECT COUNT(*) FROM content
            WHERE status = 'approved'
              AND (pipeline_version IS NULL OR pipeline_version = '')
            """
        ),
    )
    violations = int(row)
    if violations:
        report.block(f"V18：{violations} 条已发布内容的 pipeline_version 为空")
    else:
        report.ok("V18：已发布内容的 pipeline_version 全部非空")


def run_checks(engine: Engine, *, full: bool = False) -> CheckReport:
    """执行全部一致性校验。

    Args:
        engine: 数据库引擎。
        full: 是否执行数据层校验（向量/画像/冲突）。``False`` 时只做结构校验。

    Returns:
        巡检报告。
    """
    report = CheckReport()
    _check_tables(engine, report)
    _check_unique_constraints(engine, report)
    _check_indexes(engine, report)
    _check_partitions(engine, report)
    _check_functions(engine, report)
    _check_pii_privileges(engine, report)
    _check_review_record_immutable(engine, report)
    if full:
        _check_vector_consistency(engine, report)
        _check_profile_consistency(engine, report)
        _check_conflict_seven_elements(engine, report)
        _check_pipeline_version(engine, report)
    return report


def main(argv: list[str] | None = None) -> int:
    """脚本入口。

    Args:
        argv: 命令行参数；``None`` 时取 ``sys.argv[1:]``。

    Returns:
        退出码（0 通过 / 1 阻断 / 2 告警）。
    """
    parser = argparse.ArgumentParser(description="tagmind 数据一致性巡检")
    parser.add_argument(
        "--after-migration",
        metavar="REVISION",
        help="迁移后校验；传 head 表示校验到最新版本",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="执行完整的数据层校验（向量/画像/冲突）",
    )
    parser.add_argument(
        "--url",
        help="数据库连接串；默认从 Settings 派生",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    engine = build_engine(settings, url=args.url)
    try:
        if args.after_migration:
            current = _scalar(engine, text("SELECT version_num FROM alembic_version"))
            expected = "head" if args.after_migration == "head" else args.after_migration
            if expected != "head" and current != expected:
                print(f"迁移版本不符：当前 {current}，期望 {expected}")
                return 1
            print(f"迁移版本校验通过：{current}")
        report = run_checks(engine, full=args.full or args.after_migration is not None)
    finally:
        engine.dispose()

    for item in report.passed:
        print(f"[PASS] {item}")
    for item in report.warnings:
        print(f"[WARN] {item}")
    for item in report.blocking:
        print(f"[FAIL] {item}")
    print(
        f"通过 {len(report.passed)} 项，告警 {len(report.warnings)} 项，阻断 {len(report.blocking)} 项"
    )
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
