"""``python -m tagmind`` 入口。

委派给 :mod:`tagmind.cli` 的 Typer 应用，使容器与本地都能用同一条命令启动。
"""

from __future__ import annotations

from tagmind.cli import main

if __name__ == "__main__":
    main()
