"""一键灌种子数据：标签体系 + 合成内容 + 合成曝光 + 合成金标。

**数据来源声明（Q10 / Q18）**：本脚本生成的所有内容、用户、行为与金标
**全部是合成数据**，``synthetic=true`` 标记落在 ``content.synthetic`` 与
``exposure_log.synthetic`` 上。任何基于这些数据产出的指标都必须标注
``evidence_type=offline_simulation``，不得当作线上真实效果。

阶段 0 只实现**标签体系**部分；内容与行为的合成留给阶段 1/3（打标与推荐
链路就绪后才有意义——现在灌进去的曝光日志没有对应的打标结果，无法用于召回
评测）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = REPO_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from tagmind.config.settings import get_settings  # noqa: E402
from tagmind.core.logging import configure_logging, get_logger  # noqa: E402
from tagmind.db.repositories.taxonomy_repo import TaxonomyRepository  # noqa: E402
from tagmind.db.session import session_scope  # noqa: E402
from tagmind.labeling.taxonomy import load_taxonomy_from_yaml  # noqa: E402

#: 标签体系种子目录。
SEED_DIR = REPO_ROOT / "data" / "label_taxonomy"

#: 标签体系版本号。
DEFAULT_VERSION = "ld_v1"

logger = get_logger("tagmind.seed")


def seed_labels(version: str = DEFAULT_VERSION, directory: Path | None = None) -> int:
    """导入 12 层标签体系。

    幂等：同版本重复导入只会更新，不会产生重复行。

    Args:
        version: 标签体系版本。
        directory: 种子目录；``None`` 时用仓库内的默认目录。

    Returns:
        写入的标签行数。
    """
    seed_dir = directory or SEED_DIR
    tree = load_taxonomy_from_yaml(seed_dir, version=version)
    violations = tree.validate_release()
    if violations:
        for violation in violations[:20]:
            logger.error("taxonomy.violation", detail=violation)
        raise SystemExit(f"标签体系校验失败：{len(violations)} 项违规")

    logger.info("taxonomy.loaded", total=tree.total(), version=version)
    with session_scope() as session:
        repo = TaxonomyRepository(session)
        written = repo.bulk_upsert_defs(tree)
        repo.activate_version(version)
    logger.info("taxonomy.seeded", written=written, version=version)
    return written


def run_seed(
    contents: int = 1000,
    users: int = 100,
    days: int = 30,
    version: str = DEFAULT_VERSION,
) -> None:
    """执行完整种子流程。

    Args:
        contents: 合成内容条数（阶段 1 实现）。
        users: 合成用户数（阶段 3 实现）。
        days: 合成行为天数（阶段 3 实现）。
        version: 标签体系版本。
    """
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    written = seed_labels(version=version)
    logger.info(
        "seed.done",
        labels=written,
        contents=contents,
        users=users,
        days=days,
        note="内容/用户/行为合成待阶段 1 与阶段 3 实现",
    )


def main(argv: list[str] | None = None) -> int:
    """脚本入口。

    Args:
        argv: 命令行参数；``None`` 时取 ``sys.argv[1:]``。

    Returns:
        退出码。
    """
    parser = argparse.ArgumentParser(description="tagmind 种子数据灌入")
    parser.add_argument("--version", default=DEFAULT_VERSION, help="标签体系版本")
    parser.add_argument("--contents", type=int, default=1000, help="合成内容条数")
    parser.add_argument("--users", type=int, default=100, help="合成用户数")
    parser.add_argument("--days", type=int, default=30, help="合成行为天数")
    args = parser.parse_args(argv)

    run_seed(
        contents=args.contents,
        users=args.users,
        days=args.days,
        version=args.version,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
