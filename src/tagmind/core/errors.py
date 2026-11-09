"""tagmind 异常体系。

架构契约（架构文档 §14.3）：按「输入与资源 / 认证与限流 / 外部依赖 / 领域语义 /
配置与内部」五段分层，错误码唯一且段位语义稳定。

错误码段位：

======== ==========================================
段位     含义
======== ==========================================
TM_1xxx  输入 / 资源（客户端可依赖）
TM_2xxx  认证 / 限流
TM_3xxx  外部依赖（可重试性由异常类型决定）
TM_4xxx  领域语义（客户端据此做业务分支）
TM_5xxx  配置 / 内部（仅记录，不承诺稳定）
======== ==========================================

所有异常都提供 :meth:`TagmindError.to_payload`，返回统一错误体::

    {"error": {"code": ..., "message": ..., "details": {...}, "trace_id": ...}}
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AuthError",
    "BackendUnavailable",
    "ConfigError",
    "ConflictArbitrationError",
    "ConstraintViolation",
    "DatabaseError",
    "DependencyUnavailable",
    "IdempotencyConflict",
    "InternalError",
    "LLMBackendError",
    "LLMFatalError",
    "NotFoundError",
    "OptimisticLockError",
    "PermissionDenied",
    "QualityRejected",
    "RateLimitError",
    "ReviewRequired",
    "TagmindError",
    "ValidationError",
    "VectorStoreError",
]


class TagmindError(Exception):
    """所有 tagmind 异常的基类。

    Attributes:
        code: 稳定错误码，跨版本不变。
        http_status: 建议的 HTTP 状态码，由 API 层统一映射。
        details: 结构化上下文，用于前端字段级提示与日志定位。
    """

    code: str = "TM_0000"
    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        code: str | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status

    def to_payload(self, trace_id: str | None = None) -> dict[str, Any]:
        """渲染统一错误体。

        Args:
            trace_id: 全链路 ID，用于把用户看到的错误与服务端日志对上。

        Returns:
            形如 ``{"error": {"code", "message", "details", "trace_id"}}`` 的字典。
        """
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }
        if trace_id:
            error["trace_id"] = trace_id
        return {"error": error}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


# ── 输入与资源 ───────────────────────────────────────────────────────────────


class ValidationError(TagmindError):
    """入参不合法（400）。"""

    code = "TM_1000"
    http_status = 400


class NotFoundError(TagmindError):
    """资源不存在（404）。"""

    code = "TM_1001"
    http_status = 404


class IdempotencyConflict(TagmindError):
    """同一幂等键携带了不同载荷（409）。

    同一幂等键**同载荷**的重放是合法幂等行为；载荷不同则说明调用方用错了键，
    必须显式失败而不是悄悄返回旧数据。
    """

    code = "TM_1002"
    http_status = 409


class OptimisticLockError(TagmindError):
    """乐观锁版本冲突（409）。

    仓储层的条件更新未命中时抛出；调用方应重新读取后重试。
    """

    code = "TM_1003"
    http_status = 409


# ── 认证与限流 ───────────────────────────────────────────────────────────────


class AuthError(TagmindError):
    """认证失败（401）。响应体**不回显**任何密钥内容。"""

    code = "TM_2000"
    http_status = 401


class PermissionDenied(TagmindError):
    """已认证但无权限（403）。

    命名为 ``PermissionDenied`` 而非 ``PermissionError``，避免遮蔽内置异常。
    """

    code = "TM_2001"
    http_status = 403


class RateLimitError(TagmindError):
    """触发限流（429）。API 层负责补 ``Retry-After`` 头。"""

    code = "TM_2002"
    http_status = 429


# ── 外部依赖 ─────────────────────────────────────────────────────────────────


class DependencyUnavailable(TagmindError):
    """外部依赖不可用（503），**可重试**。"""

    code = "TM_3000"
    http_status = 503


class LLMBackendError(TagmindError):
    """LLM 后端调用失败（502），通常可重试。"""

    code = "TM_3001"
    http_status = 502


class LLMFatalError(LLMBackendError):
    """LLM 后端致命失败（502），**不可重试**（401/403/参数错/上下文超长）。"""

    code = "TM_3002"
    http_status = 502


class VectorStoreError(TagmindError):
    """向量存储读写失败（502）。"""

    code = "TM_3003"
    http_status = 502


class DatabaseError(TagmindError):
    """数据库操作失败（503）。"""

    code = "TM_3004"
    http_status = 503


# ── 领域语义 ─────────────────────────────────────────────────────────────────


class ConstraintViolation(TagmindError):
    """C1–C10 约束无法自动修复（422）。

    ``details`` 携带结构化违规明细（规则号、标签 code、原因）。
    """

    code = "TM_4000"
    http_status = 422


class ConflictArbitrationError(TagmindError):
    """仲裁置信度不足（422）。通常伴随转人工复核。"""

    code = "TM_4001"
    http_status = 422


class QualityRejected(TagmindError):
    """质量闸门拒绝（422）。"""

    code = "TM_4002"
    http_status = 422


class ReviewRequired(TagmindError):
    """需转人工复核（202）。

    HTTP 202 表示「已受理，等待人工」，不是错误；因此 ``http_status`` 为 202。
    """

    code = "TM_4003"
    http_status = 202


# ── 配置与内部 ───────────────────────────────────────────────────────────────


class ConfigError(TagmindError):
    """配置错误（500），**启动即失败**，不降级、不静默。"""

    code = "TM_5000"
    http_status = 500


class BackendUnavailable(TagmindError):
    """可选后端不可用（503），如 LightFM 缺失导致协同过滤降级。"""

    code = "TM_5001"
    http_status = 503


class InternalError(TagmindError):
    """内部错误（500）。对外只给 ``internal error``，不泄漏堆栈。"""

    code = "TM_5002"
    http_status = 500
