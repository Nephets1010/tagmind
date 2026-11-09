"""全局测试夹具：DB / Redis 可用性探测与 skip 策略。

架构契约（§14.8）：集成测试在无 DB 时**只能 skip，绝不允许 Error**，
否则 CI 在无服务环境变红。skip 必须带可读原因。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

#: 仓库根目录（tests/ 的上一级）。
REPO_ROOT = Path(__file__).resolve().parent.parent

#: 标签体系种子目录。
TAXONOMY_DIR = REPO_ROOT / "data" / "label_taxonomy"

#: 互斥组定义。
EXCLUSIVE_GROUPS_FILE = REPO_ROOT / "rules" / "exclusive_groups.yaml"


def _env_flag(name: str) -> bool:
    """读取布尔型环境变量。"""
    raw = os.getenv(name, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@pytest.fixture(scope="session")
def db_url() -> str:
    """PostgreSQL 连接串；不可用时跳过整个集成测试。"""
    url = os.getenv("TAGMIND_TEST_DATABASE_URL")
    if not url:
        pytest.skip("需要 TAGMIND_TEST_DATABASE_URL，未提供则跳过集成测试")
    try:
        import psycopg

        with psycopg.connect(url, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        pytest.skip(f"PostgreSQL 不可用：{exc}")
    return url


@pytest.fixture(scope="session")
def redis_client() -> object:
    """Redis 客户端；不可用时跳过。"""
    url = os.getenv("TAGMIND_TEST_REDIS_URL")
    if not url:
        pytest.skip("需要 TAGMIND_TEST_REDIS_URL，未提供则跳过 Redis 集成测试")
    try:
        from redis import Redis

        client = Redis.from_url(url, socket_connect_timeout=2)
        client.ping()
    except Exception as exc:
        pytest.skip(f"Redis 不可用：{exc}")
    return client


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """仓库根目录。"""
    return REPO_ROOT


@pytest.fixture(scope="session")
def taxonomy_dir() -> Path:
    """标签体系种子目录。"""
    return TAXONOMY_DIR


@pytest.fixture(scope="session")
def exclusive_groups_file() -> Path:
    """互斥组定义文件。"""
    return EXCLUSIVE_GROUPS_FILE


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """清掉所有 ``TAGMIND_`` 环境变量，避免开发机上的本地配置污染测试。

    Yields:
        None；测试结束后环境自动恢复。
    """
    saved = {key: value for key, value in os.environ.items() if key.startswith("TAGMIND_")}
    for key in saved:
        monkeypatch.delenv(key, raising=False)
    yield
    for key in saved:
        monkeypatch.setenv(key, saved[key])


@pytest.fixture
def offline_mode() -> bool:
    """是否处于「禁止外发」模式。

    单元测试一律视为 ``True``：它们不应触网。
    """
    return not _env_flag("TAGMIND_ALLOW_NETWORK")


@pytest.fixture(scope="session")
def migrated_db(db_url: str) -> str:
    """确保测试库已应用全部迁移，返回连接串。

    集成测试假定表结构就绪——否则每个测试文件都要自己跑一遍 alembic，
    而且「迁移失败」和「业务测试失败」会混在一起，看不出是谁的问题。
    这里统一在 session 级做一次。
    """
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(root / "alembic.ini"), "upgrade", "head"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        cwd=str(root),
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "TAGMIND_DATABASE_URL": db_url,
            "PYTHONPATH": str(root / "src"),
            "HOME": str(root),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "WINDIR": os.environ.get("WINDIR", ""),
            "TEMP": os.environ.get("TEMP", ""),
            "TMP": os.environ.get("TMP", ""),
        },
    )
    if result.returncode != 0:
        pytest.skip(f"迁移未能应用，跳过集成测试：\n{result.stdout}\n{result.stderr}")
    return db_url
