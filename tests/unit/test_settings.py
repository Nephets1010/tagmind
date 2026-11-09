"""``Settings`` 与 ``.env.example`` 的逐项对齐测试。

架构契约（§14.1）：``.env.example`` 是环境变量的**唯一真源**，配置类字段名
必须与之逐项同名。这里用「读文件 + 解析键名 + 断言字段存在」的方式强制，
而不是靠人肉核对——核对一次就会漂移。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from tagmind.config.settings import Settings, get_settings
from tagmind.core.errors import ConfigError

ENV_EXAMPLE = Path(__file__).resolve().parent.parent.parent / ".env.example"

#: ``TAGMIND_XXX=`` 形式的键名。
_KEY_RE = re.compile(r"^\s*(TAGMIND_[A-Z0-9_]+)\s*=")


def _env_example_keys() -> dict[str, str]:
    """解析 ``.env.example`` 中的所有键。

    Returns:
        键名（不含前缀） → 完整键名。
    """
    keys: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _KEY_RE.match(stripped)
        if match is None:
            continue
        full = match.group(1)
        keys[full.removeprefix("TAGMIND_").lower()] = full
    return keys


def test_env_example_exists() -> None:
    """``.env.example`` 必须存在——它是配置契约的锚点。"""
    assert ENV_EXAMPLE.is_file(), f"缺少 {ENV_EXAMPLE}"


def test_settings_cover_all_env_example_keys() -> None:
    """``Settings`` 的字段名集合必须 ⊇ ``.env.example`` 的键名集合。"""
    env_keys = _env_example_keys()
    assert env_keys, "``.env.example`` 未解析出任何 TAGMIND_ 键"

    field_names = set(Settings.model_fields)
    missing = sorted(set(env_keys) - field_names)

    assert not missing, f"Settings 缺少以下环境变量对应的字段：{missing}"


def test_settings_extra_fields_are_declared() -> None:
    """``Settings`` 相对 ``.env.example`` 多出的字段必须是已知的新增项。"""
    allowed_extras = {
        # 派生字段：架构 §14.1 规定由其他键拼装，不直接读环境。
        "resolved_database_url",
        "redis_broker_url",
        "redis_cache_url",
        "llm_provider",
        "is_production",
        "canary_enabled",
    }
    env_keys = _env_example_keys()
    extras = sorted(set(Settings.model_fields) - set(env_keys) - allowed_extras)
    assert not extras, f"Settings 出现了未登记的新增字段：{extras}"


def test_new_keys_present() -> None:
    """本轮新增的 4 个键必须在 ``.env.example`` 与 Settings 中同时存在。"""
    env_keys = _env_example_keys()
    field_names = set(Settings.model_fields)
    for key in ("role", "llm_backend", "label_canary_ratio", "label_canary_version"):
        assert key in env_keys, f"``.env.example`` 缺少 TAGMIND_{key.upper()}"
        assert key in field_names, f"Settings 缺少字段 {key}"


def test_production_without_password_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """生产环境缺数据库密码必须启动失败（NF-S05）。"""
    monkeypatch.setenv("TAGMIND_ENV", "production")
    monkeypatch.setenv("TAGMIND_POSTGRES_PASSWORD", "")
    get_settings.cache_clear()
    with pytest.raises(ConfigError) as excinfo:
        get_settings()
    assert excinfo.value.code == "TM_5000"
    assert "POSTGRES_PASSWORD" in excinfo.value.message
    get_settings.cache_clear()


def test_production_with_password_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """生产环境配了密码就应该正常启动。"""
    monkeypatch.setenv("TAGMIND_ENV", "production")
    monkeypatch.setenv("TAGMIND_POSTGRES_PASSWORD", "s3cr3t-from-vault")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.is_production
    assert settings.postgres_password.get_secret_value() == "s3cr3t-from-vault"
    get_settings.cache_clear()


def test_llm_enabled_false_with_remote_backend_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``LLM_ENABLED=false`` 且 backend 非 deterministic 时必须启动报错。

    这是防误配外发的关键闸门：运维以为在跑确定性模式，实际把正文发给了云端。
    """
    monkeypatch.setenv("TAGMIND_LLM_ENABLED", "false")
    monkeypatch.setenv("TAGMIND_LLM_BACKEND", "llm_api")
    get_settings.cache_clear()
    with pytest.raises(ConfigError) as excinfo:
        get_settings()
    assert "deterministic" in excinfo.value.message
    get_settings.cache_clear()


def test_llm_enabled_true_with_deterministic_backend_is_tolerated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``LLM_ENABLED=true`` + ``backend=deterministic`` 时以 backend 为准，不报错。"""
    monkeypatch.setenv("TAGMIND_LLM_ENABLED", "true")
    monkeypatch.setenv("TAGMIND_LLM_BACKEND", "deterministic")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.llm_backend == "deterministic"
    assert settings.llm_provider == "deterministic"
    get_settings.cache_clear()


def test_canary_ratio_out_of_range_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """灰度比例越界必须报错。"""
    monkeypatch.setenv("TAGMIND_LABEL_CANARY_RATIO", "150")
    get_settings.cache_clear()
    with pytest.raises(ConfigError):
        get_settings()
    get_settings.cache_clear()


def test_database_url_derived_from_parts() -> None:
    """``database_url`` 为空时由 ``postgres_*`` 五项派生。"""
    settings = Settings(
        postgres_host="db.internal",
        postgres_port=6543,
        postgres_db="tm",
        postgres_user="tagmind",
        postgres_password="p@ss word",
    )
    url = settings.resolved_database_url
    assert url.startswith("postgresql+psycopg://")
    assert "db.internal:6543" in url
    assert url.endswith("/tm")
    # 密码中的特殊字符必须被 URL 编码，否则连接串会被截断。
    assert " " not in url


def test_database_url_explicit_wins() -> None:
    """显式配置的 ``database_url`` 优先于派生值。"""
    explicit = "postgresql+psycopg://u:p@h:5432/d"
    settings = Settings(database_url=explicit)
    assert settings.resolved_database_url == explicit


def test_redis_db_separation() -> None:
    """Redis 三库分离（ADR-0006）：broker 走 /1，cache 走 /2。"""
    settings = Settings(redis_url="redis://localhost:6379/0")
    assert settings.redis_broker_url.endswith(":6379/1")
    assert settings.redis_cache_url.endswith(":6379/2")


def test_secrets_are_secret_str() -> None:
    """所有密钥字段必须是 ``SecretStr``，避免被日志意外渲染。"""
    for name in ("postgres_password", "api_key", "redis_password", "llm_api_key"):
        annotation = Settings.model_fields[name].annotation
        assert "SecretStr" in str(annotation), f"{name} 必须是 SecretStr"


def test_cors_origins_accept_comma_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """CORS 白名单同时接受 JSON 数组与逗号分隔字符串。"""
    monkeypatch.setenv("TAGMIND_CORS_ORIGINS", "http://a.test,http://b.test")
    settings = Settings()
    assert settings.cors_origins == ["http://a.test", "http://b.test"]


def test_log_level_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """日志级别统一大写。"""
    monkeypatch.setenv("TAGMIND_LOG_LEVEL", "debug")
    assert Settings().log_level == "DEBUG"


def test_invalid_value_raises_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """非法取值（端口非数字）必须被 pydantic 拦下。"""
    monkeypatch.setenv("TAGMIND_API_PORT", "not-a-port")
    with pytest.raises(ValidationError):
        Settings()
