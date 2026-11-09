"""tagmind — 企业级 LLM 驱动的智能打标与个性化内容分发系统。

本包的分层约束（架构文档 §14.2「铁律」）：

- ``tagmind.domain``  **零框架依赖**，只用标准库 ``dataclass`` / ``Enum``。
- ``tagmind.db``       只经仓储对外暴露领域对象，禁止 ORM 细节泄漏到编排层。
- ``tagmind.api``      使用 pydantic v2 模型，负责 JSON Schema 与 OpenAPI。

任何违反上述约束的改动都会被 ``tests/unit/test_domain_purity.py`` 的 AST 扫描拦下。
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
