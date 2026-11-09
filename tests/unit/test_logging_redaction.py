"""日志脱敏测试（NF-O03）。

架构契约（§14.4）：敏感字段必须在**渲染前**被脱敏，因此无论用哪种输出后端
都拿不到明文。这里用 JSON 输出做断言——JSON 是生产的默认格式。
"""

from __future__ import annotations

import io
import json
import re
from typing import Any

import pytest

from tagmind.core.logging import configure_logging, get_logger, reset_logging


@pytest.fixture
def log_output() -> io.StringIO:
    """配置 JSON 日志到内存流，并返回该流。"""
    stream = io.StringIO()
    configure_logging(level="INFO", fmt="json", stream=stream)
    yield stream
    reset_logging()


def _records(stream: io.StringIO) -> list[dict[str, Any]]:
    """把日志流解析成 JSON 记录列表。"""
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_api_key_is_masked(log_output: io.StringIO) -> None:
    """``api_key`` 的值必须被整值抹除。"""
    get_logger("test").info("auth", api_key="sk-xxx")
    raw = log_output.getvalue()
    assert "sk-xxx" not in raw
    assert '"api_key": "***"' in raw


def test_secret_value_in_unknown_key_is_masked(log_output: io.StringIO) -> None:
    """键名不认识时，值级密钥模式也要抹除。"""
    get_logger("test").info("call", note="sk-abcdef123456")
    raw = log_output.getvalue()
    assert "sk-abcdef123456" not in raw
    assert "***" in raw


def test_aws_key_is_masked(log_output: io.StringIO) -> None:
    """AWS AKIA 前缀密钥必须抹除。"""
    get_logger("test").info("call", credential_value="AKIAIOSFODNN7EXAMPLE")
    assert "AKIAIOSFODNN7EXAMPLE" not in log_output.getvalue()


def test_password_field_is_masked(log_output: io.StringIO) -> None:
    """``password`` 字段必须抹除。"""
    get_logger("test").info("db", password="hunter2")
    assert "hunter2" not in log_output.getvalue()


def test_authorization_field_is_masked(log_output: io.StringIO) -> None:
    """``authorization`` 字段必须抹除。"""
    get_logger("test").info("http", authorization="Bearer abcdefghijklmnop")
    assert "abcdefghijklmnop" not in log_output.getvalue()


def test_token_field_is_masked(log_output: io.StringIO) -> None:
    """``token`` 字段必须抹除。"""
    get_logger("test").info("auth", access_token="tok-1234567890")
    assert "tok-1234567890" not in log_output.getvalue()


def test_phone_is_masked(log_output: io.StringIO) -> None:
    """手机号必须替换为占位符。"""
    get_logger("test").info("contact", note="call 13812345678 now")
    raw = log_output.getvalue()
    assert "13812345678" not in raw
    assert "[PHONE_1]" in raw


def test_id_card_is_masked(log_output: io.StringIO) -> None:
    """身份证必须替换为占位符。"""
    get_logger("test").info("contact", note="id 110101199003071234 x")
    raw = log_output.getvalue()
    assert "110101199003071234" not in raw
    assert "[ID_CARD_1]" in raw


def test_email_is_masked(log_output: io.StringIO) -> None:
    """邮箱必须变成 ``a***@example.com`` 形式。"""
    get_logger("test").info("contact", note="mail to zhangsan@example.com")
    raw = log_output.getvalue()
    assert "zhangsan@example.com" not in raw
    assert "z***@example.com" in raw


def test_user_id_is_hashed(log_output: io.StringIO) -> None:
    """``user_id`` 必须哈希成 16 位短标识。"""
    get_logger("test").info("feed", user_id="user-42")
    raw = log_output.getvalue()
    assert "user-42" not in raw
    assert re.search(r'"user_id": "[0-9a-f]{16}"', raw)


def test_raw_text_not_logged(log_output: io.StringIO) -> None:
    """正文只记长度与哈希，不落原文。"""
    get_logger("test").info("clean", raw_text="这是一段很长的正文内容")
    raw = log_output.getvalue()
    assert "这是一段很长的正文内容" not in raw
    assert '"length"' in raw
    assert '"sha256"' in raw


def test_tags_only_counted(log_output: io.StringIO) -> None:
    """标签列表只记数量，不记完整列表（防日志膨胀）。"""
    get_logger("test").info("tag", tags=["C_AI", "C_CHIP", "C_PHONE"])
    raw = log_output.getvalue()
    assert "C_CHIP" not in raw
    assert '"count": 3' in raw


def test_error_truncated(log_output: io.StringIO) -> None:
    """``error`` 字段截断至 2 KB。"""
    get_logger("test").error("boom", error="x" * 5000)
    raw = log_output.getvalue()
    assert len(raw) < 4000


def test_nested_dict_is_redacted(log_output: io.StringIO) -> None:
    """嵌套字典里的敏感字段也要脱敏。"""
    get_logger("test").info("cfg", payload={"api_key": "sk-nested", "ok": 1})
    raw = log_output.getvalue()
    assert "sk-nested" not in raw
    assert '"ok": 1' in raw


def test_list_items_are_redacted(log_output: io.StringIO) -> None:
    """列表元素里的敏感值也要脱敏。"""
    get_logger("test").info("batch", items=["sk-item-1234", "safe"])
    raw = log_output.getvalue()
    assert "sk-item-1234" not in raw
    assert "safe" in raw


def test_log_has_standard_fields(log_output: io.StringIO) -> None:
    """每条日志必须带 ts / level / event / logger。"""
    get_logger("tagmind.test").info("pipeline.content.ingested", content_id=7)
    record = _records(log_output)[0]
    assert record["level"] == "info"
    assert record["event"] == "pipeline.content.ingested"
    assert record["logger"] == "tagmind.test"
    assert "timestamp" in record


def test_context_is_injected(log_output: io.StringIO) -> None:
    """contextvar 中的 request_id 必须自动注入日志。"""
    from tagmind.core.context import bind_context, clear_context

    bind_context(request_id="req-1", trace_id="trace-1")
    try:
        get_logger("test").info("hello")
    finally:
        clear_context()
    record = _records(log_output)[0]
    assert record["request_id"] == "req-1"
    assert record["trace_id"] == "trace-1"


def test_no_print_used_in_core(monkeypatch: pytest.MonkeyPatch) -> None:
    """核心模块不得使用 ``print``——一切走 ``core.logging``。"""
    import ast
    from pathlib import Path

    core_dir = Path(__file__).resolve().parent.parent.parent / "src" / "tagmind" / "core"
    offenders: list[str] = []
    for path in core_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "print":
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"core/ 中存在 print 调用：{offenders}"
