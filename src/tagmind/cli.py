"""tagmind 命令行入口（Typer）。

**子命令名是既有契约**（Makefile 已引用，不得改名）：

- ``serve``       —— 启动 API 服务（``make run``）
- ``migrate``     —— 应用数据库迁移
- ``seed-labels`` —— 导入 12 层标签体系种子
- ``seed``        —— 灌入合成数据集
- ``demo``        —— 端到端演示
- ``bench``       —— 离线推荐基准（三条基线）

本阶段（阶段 0）完整实现 ``migrate`` 与 ``seed-labels``；其余子命令已注册
并出现在 ``--help`` 中，但在对应阶段实现前会明确报错退出，而不是静默成功——
**占位符假装成功比报错更危险**。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from tagmind import __version__

app = typer.Typer(
    name="tagmind",
    help="tagmind —— 企业级 LLM 驱动的智能打标与个性化内容分发系统",
    no_args_is_help=True,
    add_completion=False,
)


def _repo_root() -> Path:
    """返回仓库根目录。"""
    return Path(__file__).resolve().parent.parent.parent


def _version_callback(value: bool) -> None:
    """打印版本后退出。"""
    if value:
        typer.echo(f"tagmind {__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True),
    ] = False,
) -> None:
    """全局选项。"""


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="监听地址")] = None,
    port: Annotated[int | None, typer.Option(help="监听端口")] = None,
    reload: Annotated[bool, typer.Option(help="开发态自动重载")] = False,
    workers: Annotated[int | None, typer.Option(help="worker 进程数")] = None,
) -> None:
    """启动 API 服务。"""
    import uvicorn

    from tagmind.config.settings import get_settings

    settings = get_settings()
    uvicorn.run(
        "tagmind.api.app:create_app",
        factory=True,
        host=host or settings.api_host,
        port=port or settings.api_port,
        reload=reload,
        workers=workers or settings.workers,
    )


@app.command()
def migrate(
    revision: Annotated[str, typer.Argument(help="目标 revision，默认 head")] = "head",
    sql: Annotated[bool, typer.Option(help="生成 SQL 脚本而不执行")] = False,
) -> None:
    """应用数据库迁移。"""
    from alembic import command
    from alembic.config import Config

    config = Config(str(_repo_root() / "alembic.ini"))
    config.set_main_option("script_location", str(_repo_root() / "migrations"))
    if sql:
        command.upgrade(config, revision, sql=True)
    else:
        command.upgrade(config, revision)
    typer.echo(f"迁移完成：{revision}")


@app.command("seed-labels")
def seed_labels(
    version: Annotated[str, typer.Option(help="标签体系版本")] = "ld_v1",
    directory: Annotated[str | None, typer.Option(help="标签体系种子目录")] = None,
    dry_run: Annotated[bool, typer.Option(help="只校验不写库")] = False,
) -> None:
    """导入 12 层标签体系种子。"""
    from tagmind.db.repositories.taxonomy_repo import TaxonomyRepository
    from tagmind.db.session import session_scope
    from tagmind.labeling.taxonomy import load_taxonomy_from_yaml

    seed_dir = directory or str(_repo_root() / "data" / "label_taxonomy")
    tree = load_taxonomy_from_yaml(seed_dir, version=version)
    violations = tree.validate_release()
    if violations:
        typer.echo(f"标签体系校验失败（{len(violations)} 项）：", err=True)
        for violation in violations[:20]:
            typer.echo(f"  - {violation}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"标签体系校验通过：{tree.total()} 个标签，版本 {tree.version}")
    if dry_run:
        return

    with session_scope() as session:
        repo = TaxonomyRepository(session)
        inserted = repo.bulk_upsert_defs(tree)
        repo.activate_version(version)
        typer.echo(f"已写入 {inserted} 个标签定义，激活版本 {version}")


@app.command()
def seed(
    contents: Annotated[int, typer.Option(help="合成内容条数")] = 1000,
    users: Annotated[int, typer.Option(help="合成用户数")] = 100,
    days: Annotated[int, typer.Option(help="合成行为天数")] = 30,
) -> None:
    """灌入合成数据集。"""
    from scripts.seed_demo import run_seed

    run_seed(contents=contents, users=users, days=days)


@app.command()
def demo() -> None:
    """运行端到端演示。"""
    typer.echo("demo 命令将在阶段 3（召回与排序）实现。", err=True)
    raise typer.Exit(code=1)


@app.command()
def bench() -> None:
    """运行离线推荐基准（三条基线）。"""
    typer.echo("bench 命令将在阶段 4（评测与回放）实现。", err=True)
    raise typer.Exit(code=1)


@app.command("eval")
def eval_cmd() -> None:
    """运行离线评测。"""
    typer.echo("eval 命令将在阶段 4（评测与回放）实现。", err=True)
    raise typer.Exit(code=1)


@app.command()
def replay() -> None:
    """运行离线回放 A/B。"""
    typer.echo("replay 命令将在阶段 4（评测与回放）实现。", err=True)
    raise typer.Exit(code=1)


@app.command()
def drift() -> None:
    """运行标签漂移抽检。"""
    typer.echo("drift 命令将在阶段 4（漂移抽检）实现。", err=True)
    raise typer.Exit(code=1)


@app.command()
def dlq() -> None:
    """查看 / 重放死信队列。"""
    typer.echo("dlq 命令将在阶段 1（Celery 队列与 DLQ）实现。", err=True)
    raise typer.Exit(code=1)


def main() -> None:
    """CLI 入口。"""
    app()


if __name__ == "__main__":
    main()
