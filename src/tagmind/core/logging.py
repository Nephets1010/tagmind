"""结构化日志（structlog）与敏感字段脱敏。

架构契约（架构文档 §14.4）：

- 每条日志至少带 ``ts`` / ``level`` / ``event`` / ``logger``。
- JSON 渲染为默认格式（生产机��只采 JSON）。
- 敏感字段按类别处理：

  ================== ============================================================
  类别                处理
  ================== ============================================================
  key/token/secret   整值替换为 ``***``
  手机号/身份证/银行卡  正则识别后替换为 ``[PHONE_1]`` / ``[ID_CARD_1]`` / ``[BANK_CARD_1]``
  邮箱                ``a***@example.com``
  user_id            ``sha256(user_id)[:16]``
  raw_text/clean_text 只记长度与哈希
  tags/tag_weights   只记数量
  error              截断至 2 KB
  ================== ============================================================

脱敏在**结构化渲染之前**完成，因此任何输出后端（JSON / console / 远端采集）
都拿到的是已脱敏的事件字典——这是把脱敏放在 processor 而非调用点的原因：
调用点总会有人忘记。
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from typing import Any, Final, TextIO, cast

import structlog

from tagmind.core.context import current_context

__all__ = [
    "configure_logging",
    "get_logger",
    "redact_event",
    "redact_mapping",
    "reset_logging",
]

#: 敏感值的统一替换文本。
REDACTED: Final[str] = "***"

#: 键名命中即整值抹除的敏感词。
_SENSITIVE_KEY_TOKENS: Final[tuple[str, ...]] = (
    "api_key",
    "apikey",
    "access_key",
    "secret",
    "password",
    "passwd",
    "token",
    "authorization",
    "credential",
    "private_key",
    "cookie",
    "session_key",
)

#: 键名以 ``_key`` 结尾或恰为 ``key`` 时也整值抹除。
_KEY_LIKE_SUFFIX: Final[str] = "_key"

#: 短键名必须精确匹配，不能做子串匹配（否则会误伤 author / ping / session_id 等）。
_SENSITIVE_EXACT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "key",
        "auth",
        "pwd",
        "pswd",
        "passphrase",
        "bearer",
        "session",
        "signature",
        "jwt",
        "otp",
        "pin",
        "client_id",
        "access_key_id",
    }
)

#: 只记长度与哈希、不落正文的字段。
_TEXT_BODY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "raw_text",
        "clean_text",
        "content",
        "body",
        "text",
        "prompt",
        "system_prompt",
        "user_prompt",
        "response_text",
        "raw",
    }
)

#: 只记数量、不落完整列表的字段。
_COUNT_ONLY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "tags",
        "tag_weights",
        "labels",
        "label_ids",
        "resolved_labels",
        "ancestors",
        "siblings",
        "children",
    }
)

#: 需要截断的长文本字段。
_TRUNCATE_KEYS: Final[frozenset[str]] = frozenset(
    {"error", "exc_info", "stack", "traceback", "exception"}
)

#: 长文本字段的最大保留长度（2 KB）。
_MAX_ERROR_CHARS: Final[int] = 2048

#: ``user_id`` 哈希后保留的长度。
_USER_ID_HASH_LEN: Final[int] = 16

#: 值级密钥模式：即使键名不认识，也一律抹除。
_SECRET_VALUE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
)

#: 堆栈 / 异常文本里的 ``password=hunter2`` 这类键值对。
#: 只匹配已知敏感键名，并保留键名本身，便于事后判断「哪一项被抹了」。
_KV_SECRET_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(password|passwd|pwd|passphrase|secret|token|api[_-]?key|"
    r"access[_-]?key|authorization|auth|credential|private[_-]?key|"
    r"client[_-]?secret|jwt|otp|session[_-]?id)\s*([=:])\s*"
    r"[\"']?([^\s,;)\&\"']{1,128})"
)


def _mask_kv_secrets(text: str) -> str:
    """抹掉自由文本里「敏感键=值」的值部分。"""
    return _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)


#: 值级 PII 模式。
_PHONE_RE: Final[re.Pattern[str]] = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID_CARD_RE: Final[re.Pattern[str]] = re.compile(r"(?<!\w)\d{17}[\dXx](?!\w)")
_BANK_CARD_RE: Final[re.Pattern[str]] = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_EMAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"([A-Za-z0-9._%+\-]+)@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})"
)

#: 各模式对应的占位符前缀。
_PLACEHOLDER_PREFIXES: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (_ID_CARD_RE, "ID_CARD"),
    (_BANK_CARD_RE, "BANK_CARD"),
    (_PHONE_RE, "PHONE"),
)


def _normalize_key(key: str) -> str:
    """把 ``api-key`` / ``X-API-Key`` / ``apiKey`` 归一成 ``api_key``。

    脱敏漏判的一个常见来源就是键名写法差异：运维传 ``X-Api-Key``、
    SDK 传 ``apiKey``、配置里写 ``api_key``，三者都该被同一套规则拦下。
    """
    import re as _re

    normalized = _re.sub(r"[-.\s]", "_", key.strip())
    normalized = _re.sub(r"(?<!^)(?=[A-Z])", "_", normalized).lower()
    return _re.sub(r"_+", "_", normalized)


def _is_sensitive_key(key: str) -> bool:
    """判断键名是否应整值抹除。"""
    lowered = _normalize_key(key)
    if lowered in _TEXT_BODY_KEYS or lowered in _COUNT_ONLY_KEYS:
        return False
    if lowered in _SENSITIVE_EXACT_KEYS:
        return True
    if any(token in lowered for token in _SENSITIVE_KEY_TOKENS):
        return True
    # 注释约定「恰为 key 也抹除」，此前只判 endswith("_key") 会漏掉 "key"。
    return lowered == "key" or lowered.endswith(_KEY_LIKE_SUFFIX)


def _mask_pii_in_text(text: str) -> str:
    """把文本中的手机号 / 身份证 / 银行卡 / 邮箱替换为占位符。

    占位符按出现顺序编号（``[PHONE_1]``、``[PHONE_2]`` …），与 §5.7
    ``pii_mask_map.placeholder`` 的写法保持一致，方便回溯。
    """
    counters: dict[str, int] = {}
    result = text
    for pattern, prefix in _PLACEHOLDER_PREFIXES:

        def _replace(match: re.Match[str], prefix: str = prefix) -> str:
            counters[prefix] = counters.get(prefix, 0) + 1
            return f"[{prefix}_{counters[prefix]}]"

        result = pattern.sub(_replace, result)

    def _mask_email(match: re.Match[str]) -> str:
        local, domain = match.group(1), match.group(2)
        head = local[:1]
        return f"{head}***@{domain}"

    return _EMAIL_RE.sub(_mask_email, result)


def _mask_user_id(value: str) -> str:
    """把 user_id 哈希为不可逆的短标识。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_USER_ID_HASH_LEN]


def _text_digest(value: str) -> dict[str, Any]:
    """把正文换成「长度 + 短哈希」摘要。"""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return {"length": len(value), "sha256": digest}


def _redact_value(key: str, value: Any) -> Any:
    """按键名与值类型做脱敏。"""
    # 敏感键名优先整值抹除——这是最严格的一档，不能被后续分支绕过。
    if _is_sensitive_key(key):
        return REDACTED

    if key.lower() in _TEXT_BODY_KEYS and isinstance(value, str):
        return _text_digest(value)

    if key.lower() in _COUNT_ONLY_KEYS:
        if isinstance(value, dict | list | tuple | set):
            return {"count": len(value)}
        if isinstance(value, int | float):
            return value
        return {"count": 0}

    if _normalize_key(key) in _TRUNCATE_KEYS and isinstance(value, str):
        masked = value
        for pattern in _SECRET_VALUE_PATTERNS:
            masked = pattern.sub(REDACTED, masked)
        masked = _mask_kv_secrets(masked)
        return _mask_pii_in_text(masked)[:_MAX_ERROR_CHARS]

    if key.lower() == "user_id" and isinstance(value, str):
        return _mask_user_id(value)

    if isinstance(value, str):
        masked = value
        for pattern in _SECRET_VALUE_PATTERNS:
            masked = pattern.sub(REDACTED, masked)
        masked = _mask_kv_secrets(masked)
        return _mask_pii_in_text(masked)

    if isinstance(value, dict):
        return {k: _redact_value(str(k), v) for k, v in value.items()}

    if isinstance(value, set | frozenset):
        # 集合无序，直接 str() 会让日志不稳定；排序后逐项脱敏。
        return [_redact_value(key, item) for item in sorted(value, key=str)]

    if isinstance(value, list | tuple):
        return [_redact_value(key, item) for item in value]

    return value


def redact_mapping(event_dict: dict[str, Any]) -> dict[str, Any]:
    """对事件字典做全量脱敏（原地修改）。

    Args:
        event_dict: 待脱敏的事件字典。

    Returns:
        脱敏后的事件字典（与入参同一对象）。
    """
    for key in list(event_dict):
        event_dict[key] = _redact_value(key, event_dict[key])
    return event_dict


def redact_event(_logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor：在渲染前完成脱敏。

    脱敏放在 processor 而不是调用点，是因为调用点总会有人忘记——只有放在
    渲染链上，才能保证**任何**输出后端都拿不到明文（架构 §14.4）。

    Args:
        _logger: structlog 传入的 logger（未使用）。
        _method_name: structlog 传入的方法名（未使用）。
        event_dict: 组装后、渲染前的事件字典。

    Returns:
        脱敏后的事件字典。
    """
    return redact_mapping(event_dict)


def _inject_context(_logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """把 tagmind 自己的 contextvar 注入日志字段。"""
    context = current_context()
    if context is None:
        return event_dict
    for key, value in context.to_log_fields().items():
        event_dict.setdefault(key, value)
    return event_dict


def _drop_color_message(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """console 渲染前移除 ANSI 颜色标记依赖。"""
    event_dict.pop("color_message", None)
    return event_dict


class _StreamLoggerFactory:
    """按名字创建绑定到指定 handler 的标准库 logger。

    为什么不用 ``structlog.PrintLoggerFactory``：``structlog.stdlib.add_logger_name``
    需要一个带 ``.name`` 属性的标准库 logger，而 ``PrintLogger`` 没有；
    用标准库 logger 还能顺带让 uvicorn / celery 的日志与本系统日志格式一致。
    """

    def __init__(self, handler: logging.Handler, level: int) -> None:
        self._handler = handler
        self._level = level

    def __call__(self, *args: object) -> logging.Logger:
        """返回（或创建）具名 logger。"""
        name = str(args[0]) if args else "tagmind"
        logger = logging.getLogger(name)
        logger.handlers = [self._handler]
        logger.setLevel(self._level)
        logger.propagate = False
        return logger


def _add_ts_alias(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """补齐架构 §14.4 约定的 ``ts`` 字段。

    ``TimeStamper`` 默认写 ``timestamp``。为了让既有日志消费者不破，
    这里保留 ``timestamp`` 并**额外**提供同值的 ``ts``，而不是直接改键名。
    """
    if "timestamp" in event_dict:
        event_dict.setdefault("ts", event_dict["timestamp"])
    return event_dict


def configure_logging(
    *,
    level: str | None = None,
    fmt: str | None = None,
    stream: TextIO | None = None,
) -> None:
    """配置全局 structlog。

    Args:
        level: 日志级别，默认从 ``Settings`` 读取（未配置时用 ``INFO``）。
        fmt: ``json`` 或 ``console``，默认从 ``Settings`` 读取。
        stream: 输出流；测试可传入 ``StringIO`` 捕获输出。
    """
    if level is None or fmt is None:
        from tagmind.config.settings import get_settings

        settings = get_settings()
        level = level or settings.log_level
        fmt = fmt or settings.log_format

    resolved_level = _resolve_level(level)
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))

    renderer: Any = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _inject_context,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_ts_alias,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        # 必须先展开堆栈再脱敏：否则 exc_info=True 生成的 exception 字符串
        # 绕过 redact_event，明文密钥会随 5xx 堆栈落到日志里。
        redact_event,
        structlog.processors.UnicodeDecoder(),
        _drop_color_message,
        renderer,
    ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=_StreamLoggerFactory(handler, resolved_level),
        cache_logger_on_first_use=False,
    )


def _resolve_level(level: str) -> int:
    """把级别名解析成 logging 常量；未知级别退化为 ``INFO``。"""
    resolved = logging.getLevelNamesMapping().get(level.upper())
    return resolved if resolved is not None else logging.INFO


def reset_logging() -> None:
    """重置 structlog 配置（测试用）。"""
    structlog.reset_defaults()


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """取一个绑定好的 logger。

    Args:
        name: 通常是模块名 ``__name__``。

    Returns:
        已绑定的 structlog logger。
    """
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))
