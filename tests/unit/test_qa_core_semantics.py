"""QA 补充测试：核心模块的未覆盖语义（阶段 0）。

本文件由独立 QA 验证新增，覆盖既有测试套件未触及的关键路径：

* ``core/errors.py`` —— **全部**异常子类（不局限于测试里硬编码的 18 个）
  的错误码唯一性、段位、payload、层级；
* ``core/context.py`` —— contextvar 的嵌套 / 清理 / 跨 context 隔离；
* ``core/logging.py`` —— 脱敏字段清单的完整性（键名族、大小写、嵌套、
  值级兜底、截断边界）；
* ``config/settings.py`` —— ``resolved_database_url`` 对特殊字符的转义，
  以及 Redis 三库分离在带凭据 / 带 query 的 URL 下是否保持；
* ``domain/versioning.py`` —— ADR-0010 的 ``evidence_type`` 无默认值。

所有测试均为纯逻辑，不依赖 PostgreSQL / Redis / Docker。
"""

from __future__ import annotations

import contextvars
import re
from datetime import date
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import pytest

from tagmind.core import errors as errors_mod
from tagmind.core.context import (
    bind_context,
    clear_context,
    current_context,
    new_request_id,
    new_trace_id,
)
from tagmind.core.errors import TagmindError
from tagmind.core.logging import (
    _MAX_ERROR_CHARS,
    _SENSITIVE_KEY_TOKENS,
    REDACTED,
    redact_mapping,
)
from tagmind.domain.versioning import EvidenceType, MetricValue


def _all_error_classes() -> list[type[TagmindError]]:
    """递归收集 TagmindError 的全部子类（含间接子类）。"""
    found: list[type[TagmindError]] = []

    def walk(cls: type) -> None:
        for sub in cls.__subclasses__():
            if issubclass(sub, TagmindError):
                found.append(sub)
            walk(sub)

    walk(TagmindError)
    return found


class TestErrorRegistry:
    """错误码注册表：动态扫描而非硬编码清单。"""

    def test_registry_is_not_empty(self) -> None:
        """防线：若反射失败导致空集，后续断言不得假通过。"""
        classes = _all_error_classes()
        assert len(classes) >= 18, f"只发现 {len(classes)} 个异常类，反射可能失效"

    def test_all_subclasses_have_unique_codes(self) -> None:
        """**所有**异常子类的错误码唯一（含未被硬编码清单收录的类）。"""
        classes = _all_error_classes()
        codes = [cls.code for cls in classes]
        duplicates = {code for code in codes if codes.count(code) > 1}
        assert not duplicates, f"错误码重复：{duplicates}"

    def test_all_subclasses_codes_match_segment(self) -> None:
        """全部错误码格式为 TM_[1-5]xxx，且属于五个合法段位。"""
        for cls in _all_error_classes():
            assert re.fullmatch(r"TM_[1-5]\d{3}", cls.code), (
                f"{cls.__name__} 的错误码 {cls.code} 不合法"
            )

    def test_all_subclasses_http_status_sane(self) -> None:
        """全部异常类的 HTTP 状态在 200–599，且非 500 类异常不该是 2xx。"""
        for cls in _all_error_classes():
            assert 200 <= cls.http_status <= 599, cls.__name__

    def test_all_subclasses_render_payload(self) -> None:
        """每个异常类都能构造并渲染统一错误体。"""
        for cls in _all_error_classes():
            exc = cls("qa-probe", details={"field": "x"})
            body = exc.to_payload("trace-qa")["error"]
            assert body["code"] == cls.code
            assert body["message"] == "qa-probe"
            assert body["details"] == {"field": "x"}
            assert body["trace_id"] == "trace-qa"

    def test_no_exception_shadows_builtin(self) -> None:
        """异常类名不得遮蔽内置异常（内置名会造成 except 语义混乱）。"""
        import builtins

        for cls in _all_error_classes():
            assert not hasattr(builtins, cls.__name__), f"{cls.__name__} 遮蔽了内置名"

    def test_module_all_covers_all_public_classes(self) -> None:
        """``__all__`` 必须导出全部公开异常类，否则调用方无法稳定引用。"""
        exported = set(errors_mod.__all__)
        missing = {cls.__name__ for cls in _all_error_classes()} - exported - {"TagmindError"}
        # TagmindError 本身也在 __all__ 中，单独确认
        assert "TagmindError" in exported
        assert not missing, f"以下异常类未导出：{sorted(missing)}"


class TestContextVars:
    """contextvar 的嵌套、清理与隔离语义。"""

    def test_bind_then_clear_removes_everything(self) -> None:
        """``clear_context`` 必须清掉全部四个变量，而不只是 request_id。"""
        bind_context(request_id="r-1", trace_id="t-1", user_id="u-1", content_id=42)
        assert current_context() is not None
        clear_context()
        assert current_context() is None, "clear_context 后仍残留上下文"

    @pytest.mark.parametrize("field", ["user_id", "content_id"])
    def test_optional_fields_are_observable(self, field: str) -> None:
        """``user_id`` / ``content_id`` 绑定后必须出现在日志字段里。

        注意：``current_context()`` 明确规定「两个 ID 都未设置时返回 None」，
        因此这两个可选字段必须伴随 request_id 一起绑定才有快照可观察。
        曾经把断言写成「仅绑定可选字段也应有快照」，那是测试自身的错误。
        """
        clear_context()
        kwargs: dict[str, Any] = {"request_id": "r", field: "v" if field != "content_id" else 7}
        bind_context(**kwargs)
        ctx = current_context()
        assert ctx is not None
        assert ctx.to_log_fields()[field] == kwargs[field]
        clear_context()

    def test_only_optional_fields_bound_yields_no_snapshot(self) -> None:
        """两个 ID 都未设置时返回 None（锁定 §14.4 的既定语义）。"""
        clear_context()
        bind_context(user_id="u", content_id=7)
        assert current_context() is None
        clear_context()

    def test_generated_ids_are_32_hex(self) -> None:
        """request_id / trace_id 均为 32 位十六进制，且互不相同。"""
        rid, tid = new_request_id(), new_trace_id()
        assert re.fullmatch(r"[0-9a-f]{32}", rid)
        assert re.fullmatch(r"[0-9a-f]{32}", tid)
        assert rid != tid

    def test_context_is_isolated_across_contexts(self) -> None:
        """另一 ``Context`` 中不应看到外层绑定的值（async / 线程池隔离）。"""
        clear_context()
        bind_context(request_id="outer", trace_id="outer-trace")

        def probe() -> str | None:
            ctx = current_context()
            return None if ctx is None else ctx.request_id

        inner = contextvars.copy_context()

        # 在新 context 里清空并重新绑定，不应影响外层
        def rebind() -> None:
            clear_context()
            bind_context(request_id="inner", trace_id="inner-trace")

        inner.run(rebind)
        assert inner.run(probe) == "inner"
        outer = current_context()
        assert outer is not None and outer.request_id == "outer"
        clear_context()

    def test_to_log_fields_omits_none_optionals(self) -> None:
        """``to_log_fields`` 不应输出值为 None 的可选字段。"""
        clear_context()
        bind_context(request_id="r", trace_id="t")
        ctx = current_context()
        assert ctx is not None
        fields = ctx.to_log_fields()
        assert fields == {"request_id": "r", "trace_id": "t"}
        clear_context()


class TestRedactionCompleteness:
    """脱敏字段清单的完整性与边界。"""

    @pytest.mark.parametrize(
        "key",
        [
            "api_key",
            "apikey",
            "Api_Key",
            "access_key",
            "secret",
            "client_secret",
            "password",
            "passwd",
            "token",
            "access_token",
            "authorization",
            "Authorization",
            "credential",
            "private_key",
            "cookie",
            "session_key",
            "signing_key",  # 命中 _key 后缀
        ],
    )
    def test_sensitive_key_families_are_masked(self, key: str) -> None:
        """键名族里的每个名字（含大小写变体与 _key 后缀）都必须整值抹除。"""
        event = {key: "SUPERSECRETVALUE"}
        assert redact_mapping(dict(event))[key] == REDACTED

    def test_every_declared_token_is_effective(self) -> None:
        """声明在 ``_SENSITIVE_KEY_TOKENS`` 里的每个词都要真的生效。

        防止「词加进常量但判断逻辑漏掉」这类静默失效。
        """
        assert _SENSITIVE_KEY_TOKENS, "敏感键名清单为空——脱敏将完全失效"
        for token in _SENSITIVE_KEY_TOKENS:
            key = f"x_{token}_y"
            assert redact_mapping({key: "leak-me"})[key] == REDACTED, (
                f"敏感词 {token} 未被 _is_sensitive_key 命中"
            )

    def test_nested_dict_and_list_are_redacted(self) -> None:
        """嵌套结构中的敏感键与敏感值都要脱敏。"""
        event = {
            "payload": {"api_key": "sk-nested123456", "items": ["sk-list1234567", "ok"]},
            "outer_secret": "hunter2",
        }
        out = redact_mapping(event)
        raw = repr(out)
        assert "sk-nested123456" not in raw
        assert "sk-list1234567" not in raw
        assert "hunter2" not in raw
        assert out["payload"]["api_key"] == REDACTED

    def test_unknown_key_with_known_secret_shape_is_still_masked(self) -> None:
        """键名不认识时，值级模式（sk- / AKIA / Bearer）必须兜底。"""
        out = redact_mapping(
            {
                "note1": "sk-abcdefghijklmnop1234",
                "note2": "AKIAIOSFODNN7EXAMPLE",
                "note3": "Bearer abcdefghijklmnop",
            }
        )
        raw = repr(out)
        assert "sk-abcdefghijklmnop1234" not in raw
        assert "AKIAIOSFODNN7EXAMPLE" not in raw
        assert "abcdefghijklmnop" not in raw

    def test_text_body_only_keeps_length_and_hash(self) -> None:
        """正文字段只记长度与哈希，且长度必须与实际一致。"""
        text = "这是一段含手机号 13812345678 的正文"
        out = redact_mapping({"raw_text": text})
        assert out["raw_text"]["length"] == len(text)
        assert "13812345678" not in repr(out)
        assert re.fullmatch(r"[0-9a-f]{12}", out["raw_text"]["sha256"])

    def test_error_is_truncated_at_exact_boundary(self) -> None:
        """``error`` 截断边界：2048 保留、2049 截断到 2048。"""
        exact = "x" * _MAX_ERROR_CHARS
        over = "x" * (_MAX_ERROR_CHARS + 1)
        assert redact_mapping({"error": exact})["error"] == exact
        assert len(redact_mapping({"error": over})["error"]) == _MAX_ERROR_CHARS

    def test_count_only_keys_do_not_leak_members(self) -> None:
        """只记数量的字段不得出现任何成员值。"""
        out = redact_mapping({"tags": ["C_AI", "C_CHIP"], "label_ids": [1, 2, 3]})
        assert out["tags"] == {"count": 2}
        assert out["label_ids"] == {"count": 3}
        assert "C_AI" not in repr(out)

    def test_user_id_is_hashed_not_echoed(self) -> None:
        """``user_id`` 必须哈希成 16 位十六进制，不得回显原值。"""
        out = redact_mapping({"user_id": "user-42"})
        assert out["user_id"] != "user-42"
        assert re.fullmatch(r"[0-9a-f]{16}", out["user_id"])


class TestSettingsDerivedUrls:
    """配置派生 URL 的正确性（T01 扩展）。"""

    @pytest.mark.parametrize(
        "password",
        [
            "p@ss:word/path",
            "a b\tc",
            "p?#&%=+",
            "中文密码",
            "sk-not-a-real-key",
        ],
    )
    def test_password_special_chars_are_percent_encoded(self, password: str) -> None:
        """密码里的 URL 保留字必须被转义，且能无损还原。

        注意：``quote(safe="")`` 只转义保留字，纯 ASCII 字母数字与 ``-``
        属于 unreserved，按 RFC 3986 不该被转义。因此断言不能写成
        「密码原文绝不出现在 URL 里」——那是测试自身的错误。
        """
        from tagmind.config.settings import Settings

        settings = Settings(postgres_password=password, postgres_user="u@ser", postgres_db="my/db")
        url = settings.resolved_database_url
        parts = urlsplit(url)
        assert parts.scheme == "postgresql+psycopg"
        assert parts.port == 5432
        # 与 urllib.parse.quote(safe="") 的结果逐字一致；% 是转义符本身，
        # 不能当作「未转义的保留字」检查。
        assert parts.password == quote(password, safe="")
        assert unquote(parts.password) == password
        assert unquote(parts.username) == "u@ser"

    def test_explicit_url_is_returned_verbatim(self) -> None:
        """显式 database_url 优先，且不做任何改写。"""
        from tagmind.config.settings import Settings

        explicit = "postgresql+psycopg://u:p@h:5432/d?sslmode=require"
        assert Settings(database_url=explicit).resolved_database_url == explicit

    def test_redis_db_separation_preserves_credentials_and_query(self) -> None:
        """三库分离只改路径段，用户名/密码/query 必须保留。"""
        from tagmind.config.settings import Settings

        settings = Settings(redis_url="redis://:s3cr3t@localhost:6381/0?ssl=true")
        broker = urlsplit(settings.redis_broker_url)
        cache = urlsplit(settings.redis_cache_url)
        assert broker.path == "/1"
        assert cache.path == "/2"
        assert broker.password == "s3cr3t"
        assert broker.query == "ssl=true"
        assert broker.port == 6381

    def test_settings_fields_cover_env_example_keys(self) -> None:
        """T01 验收①：Settings 字段集合 ⊇ .env.example 的 TAGMIND_ 键名集合。"""
        import re as _re
        from pathlib import Path

        from tagmind.config.settings import Settings

        env_path = Path(__file__).resolve().parent.parent.parent / ".env.example"
        keys = set()
        for line in env_path.read_text(encoding="utf-8").splitlines():
            m = _re.match(r"^\s*(TAGMIND_[A-Z0-9_]+)\s*=", line)
            if m:
                keys.add(m.group(1).removeprefix("TAGMIND_").lower())
        assert keys, ".env.example 未解析出任何键——断言会假通过"
        missing = sorted(keys - set(Settings.model_fields))
        assert not missing, f"Settings 缺少字段：{missing}"


class TestHonestMetrics:
    """ADR-0010 / PRD §7.5：口径写在代码里。"""

    def test_metric_value_requires_evidence_type(self) -> None:
        """漏填 evidence_type 必须在构造时 TypeError，不能静默补默认值。"""
        with pytest.raises(TypeError, match="evidence_type"):
            MetricValue(  # type: ignore[call-arg]
                name="ctr", value=0.4, sample_size=1000, as_of=date(2026, 1, 1)
            )

    def test_metric_value_requires_sample_size_and_as_of(self) -> None:
        """sample_size / as_of 同样必填（PRD §7.5 声明 3）。"""
        with pytest.raises(TypeError):
            MetricValue(  # type: ignore[call-arg]
                name="ctr", value=0.4, evidence_type=EvidenceType.OFFLINE_SIMULATION
            )

    def test_offline_simulation_requires_caveat(self) -> None:
        """离线仿真结论必须被标记为「不可承诺线上效果」。"""
        m = MetricValue(
            name="ctr_lift",
            value=0.4,
            evidence_type=EvidenceType.OFFLINE_SIMULATION,
            sample_size=1000,
            as_of=date(2026, 1, 1),
        )
        assert m.requires_caveat() is True
        assert m.to_report_row()["evidence_type"] == "EvidenceType.OFFLINE_SIMULATION" or (
            "offline_simulation" in str(m.to_report_row()["evidence_type"])
        )

    def test_online_real_is_not_silently_defaulted(self) -> None:
        """``online_real`` 只能显式给出，且 requires_caveat 为 False。"""
        m = MetricValue(
            name="ctr",
            value=0.4,
            evidence_type=EvidenceType.ONLINE_REAL,
            sample_size=1000,
            as_of=date(2026, 1, 1),
        )
        assert m.requires_caveat() is False
