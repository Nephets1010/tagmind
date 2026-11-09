"""领域层纯净性测试：AST 扫描确认无框架 import。

架构契约（§14.2「铁律」）：``domain/`` 层零框架依赖。

这条红线靠约定守不住——总有人会在领域模型里 ``import pydantic`` 图省事。
因此用 AST 扫描强制：任何 ``import`` / ``from ... import`` 命中禁列模块即失败。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

DOMAIN_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "tagmind" / "domain"

#: 领域层禁止导入的模块（架构 §14.2 明确列出）。
FORBIDDEN_MODULES: frozenset[str] = frozenset(
    {
        "fastapi",
        "sqlalchemy",
        "pydantic",
        "pydantic_settings",
        "numpy",
        "celery",
        "redis",
        "psycopg",
        "httpx",
        "alembic",
        "structlog",
        "typer",
        "uvicorn",
        "starlette",
        "prometheus_client",
        "tenacity",
        "yaml",
        "torch",
        "lightfm",
    }
)

#: 领域层允许导入的顶层模块（标准库白名单）。
ALLOWED_STDLIB: frozenset[str] = frozenset(
    {
        "__future__",
        "abc",
        "collections",
        "collections.abc",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "functools",
        "hashlib",
        "itertools",
        "json",
        "math",
        "re",
        "statistics",
        "typing",
        "uuid",
    }
)


def _domain_files() -> list[Path]:
    """返回领域层的所有 Python 文件。"""
    return sorted(DOMAIN_DIR.rglob("*.py"))


def test_domain_dir_exists() -> None:
    """领域层目录必须存在（否则测试会假通过）。"""
    assert DOMAIN_DIR.is_dir()
    assert len(_domain_files()) >= 10


def _iter_imports(tree: ast.AST) -> list[tuple[str, int]]:
    """抽取一棵 AST 中所有 import 的根模块名与行号。"""
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # 相对导入：只取模块名部分。
                if node.module:
                    found.append((node.module.split(".")[0], node.lineno))
                continue
            if node.module:
                found.append((node.module.split(".")[0], node.lineno))
    return found


@pytest.mark.parametrize("path", _domain_files(), ids=lambda p: p.name)
def test_domain_has_no_framework_imports(path: Path) -> None:
    """领域层不得 import 任何框架或基础设施依赖。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    for module, lineno in _iter_imports(tree):
        if module in FORBIDDEN_MODULES:
            violations.append(f"{path.name}:{lineno} import {module}")
    assert not violations, "领域层出现框架依赖：\n" + "\n".join(violations)


@pytest.mark.parametrize("path", _domain_files(), ids=lambda p: p.name)
def test_domain_imports_only_stdlib_or_self(path: Path) -> None:
    """领域层只允许导入标准库与自身模块。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    for module, lineno in _iter_imports(tree):
        if module in FORBIDDEN_MODULES:
            continue
        if module in ALLOWED_STDLIB:
            continue
        if module == "tagmind":
            continue
        # 允许 tagmind 内部模块（domain/base 等）
        if module.startswith("tagmind"):
            continue
        violations.append(f"{path.name}:{lineno} import {module}")
    assert not violations, "领域层出现非标准库依赖：\n" + "\n".join(violations)


def test_domain_uses_only_dataclass_and_enum() -> None:
    """领域模型只用 ``dataclass`` 与 ``Enum`` 构造（不用 pydantic BaseModel）。"""
    violations: list[str] = []
    for path in _domain_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    base_name = ""
                    if isinstance(base, ast.Name):
                        base_name = base.id
                    elif isinstance(base, ast.Attribute):
                        base_name = base.attr
                    if base_name in {"BaseModel", "BaseSettings", "DeclarativeBase", "Mixin"}:
                        violations.append(
                            f"{path.name}:{node.lineno} class {node.name}({base_name})"
                        )
    assert not violations, f"领域层出现框架基类：{violations}"


def test_domain_models_are_frozen() -> None:
    """领域 dataclass 应当是 ``frozen=True``——不可变才守得住不变量。"""
    import dataclasses

    violations: list[str] = []
    for path in _domain_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            decorators = {
                dec.id if isinstance(dec, ast.Name) else getattr(dec, "attr", "")
                for dec in node.decorator_list
            }
            if "dataclass" not in decorators:
                continue
            frozen = False
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and getattr(dec.func, "id", "") == "dataclass":
                    for kw in dec.keywords:
                        if kw.arg == "frozen" and isinstance(kw.value, ast.Constant):
                            frozen = bool(kw.value.value)
            if not frozen:
                violations.append(f"{path.name}:{node.lineno} class {node.name}")
    assert not violations, f"领域 dataclass 缺少 frozen=True：{violations}"
    assert dataclasses  # 保持导入被使用（避免 lint 误判）


def test_domain_package_imports_cleanly() -> None:
    """领域包必须能被独立导入（不触发任何框架初始化）。"""
    import os
    import subprocess
    import sys

    src_dir = str(DOMAIN_DIR.parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = src_dir + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import tagmind.domain as d; print(len(d.__all__))",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) > 0
