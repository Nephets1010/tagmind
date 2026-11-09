"""异常体系测试：层级、错误码唯一性、payload 结构。

架构契约（§14.3）：异常按五段分层，错误码唯一，每个异常都能渲染统一错误体。
"""

from __future__ import annotations

import inspect

import pytest

from tagmind.core import errors
from tagmind.core.errors import (
    AuthError,
    BackendUnavailable,
    ConfigError,
    ConflictArbitrationError,
    ConstraintViolation,
    DatabaseError,
    DependencyUnavailable,
    IdempotencyConflict,
    InternalError,
    LLMBackendError,
    LLMFatalError,
    NotFoundError,
    OptimisticLockError,
    PermissionDenied,
    QualityRejected,
    RateLimitError,
    ReviewRequired,
    TagmindError,
    ValidationError,
    VectorStoreError,
)

#: 架构 §14.3 要求的全部异常类。
REQUIRED_ERRORS: tuple[type[TagmindError], ...] = (
    ValidationError,
    NotFoundError,
    IdempotencyConflict,
    AuthError,
    PermissionDenied,
    RateLimitError,
    DependencyUnavailable,
    LLMBackendError,
    LLMFatalError,
    VectorStoreError,
    DatabaseError,
    ConstraintViolation,
    ConflictArbitrationError,
    QualityRejected,
    ReviewRequired,
    ConfigError,
    BackendUnavailable,
    InternalError,
)


def test_all_required_errors_exist() -> None:
    """§14.3 列出的异常类必须全部存在。"""
    for error_cls in REQUIRED_ERRORS:
        assert issubclass(error_cls, TagmindError)


def test_error_codes_unique() -> None:
    """错误码必须唯一。"""
    codes = [error_cls.code for error_cls in REQUIRED_ERRORS]
    duplicates = {code for code in codes if codes.count(code) > 1}
    assert not duplicates, f"错误码重复：{duplicates}"


def test_error_codes_match_segment_rules() -> None:
    """错误码段位必须符合规则：1xxx 输入、2xxx 认证、3xxx 外部依赖、4xxx 领域、5xxx 配置。"""
    for error_cls in REQUIRED_ERRORS:
        code = error_cls.code
        assert code.startswith("TM_"), f"{error_cls.__name__} 的错误码格式错误：{code}"
        segment = int(code[3:4])
        assert segment in {1, 2, 3, 4, 5}, f"{error_cls.__name__} 的段位非法：{code}"


def test_http_status_sane() -> None:
    """HTTP 状态码必须在合法范围内。"""
    for error_cls in REQUIRED_ERRORS:
        assert 200 <= error_cls.http_status <= 599


def test_llm_fatal_is_llm_backend_subclass() -> None:
    """``LLMFatalError`` 必须是 ``LLMBackendError`` 的子类（可重试性靠类型区分）。"""
    assert issubclass(LLMFatalError, LLMBackendError)
    assert LLMFatalError.code != LLMBackendError.code


def test_to_payload_shape() -> None:
    """``to_payload`` 必须产出统一错误体。"""
    error = ValidationError(
        "raw_text must not be empty",
        details={"field": "raw_text", "reason": "empty"},
    )
    payload = error.to_payload("trace-123")
    assert set(payload) == {"error"}
    body = payload["error"]
    assert body["code"] == "TM_1000"
    assert body["message"] == "raw_text must not be empty"
    assert body["details"] == {"field": "raw_text", "reason": "empty"}
    assert body["trace_id"] == "trace-123"


def test_to_payload_without_trace_id() -> None:
    """未提供 trace_id 时不应出现该键。"""
    payload = NotFoundError("content not found").to_payload()
    assert "trace_id" not in payload["error"]


def test_details_default_empty_dict() -> None:
    """未提供 details 时为空字典而不是 None。"""
    assert InternalError("boom").details == {}


def test_custom_code_override() -> None:
    """允许按实例覆盖错误码（用于第三方扩展）。"""
    error = TagmindError("custom", code="TM_9999", http_status=418)
    assert error.code == "TM_9999"
    assert error.http_status == 418


def test_exception_message_is_readable() -> None:
    """``str(exc)`` 必须含原始消息，便于日志排查。"""
    assert "boom" in str(InternalError("boom"))


def test_all_errors_have_docstring() -> None:
    """每个异常类都要有 docstring——它们是公开 API 的一部分。"""
    for error_cls in REQUIRED_ERRORS:
        assert error_cls.__doc__, f"{error_cls.__name__} 缺少 docstring"


def test_errors_module_exports() -> None:
    """``__all__`` 中列出的名字必须真实存在。"""
    for name in errors.__all__:
        assert hasattr(errors, name), f"errors.__all__ 中的 {name} 不存在"


def test_permission_error_does_not_shadow_builtin() -> None:
    """``PermissionDenied`` 不得遮蔽内置 ``PermissionError``。"""
    import builtins

    assert PermissionDenied is not builtins.PermissionError
    assert PermissionDenied.__name__ == "PermissionDenied"


def test_review_required_is_202() -> None:
    """``ReviewRequired`` 表示「已受理待人工」，HTTP 状态是 202。"""
    assert ReviewRequired.http_status == 202


@pytest.mark.parametrize(
    "error_cls",
    [
        ValidationError,
        NotFoundError,
        AuthError,
        PermissionDenied,
        RateLimitError,
        DependencyUnavailable,
        ConstraintViolation,
        QualityRejected,
        ConfigError,
        InternalError,
        OptimisticLockError,
        BackendUnavailable,
        DatabaseError,
        VectorStoreError,
        LLMBackendError,
        ConflictArbitrationError,
    ],
)
def test_constructible_with_message_only(error_cls: type[TagmindError]) -> None:
    """所有异常都能只传一个 message 构造。"""
    error = error_cls("boom")
    assert error.message == "boom"
    assert inspect.isclass(error_cls)
