"""``TAGMIND_*`` 环境变量 → :class:`Settings` 的唯一映射点。

设计契约（架构文档 §14.1）：

1. ``.env.example`` 是环境变量的**唯一真源**，字段名与键名逐项同名
   （仅剥离 ``TAGMIND_`` 前缀并转小写）。
2. 生产环境缺 ``TAGMIND_POSTGRES_PASSWORD`` → 抛 :class:`ConfigError`，
   绝不以空密钥静默启动（NF-S05）。
3. ``TAGMIND_LLM_BACKEND`` 是 LLM 后端的**唯一权威**；``TAGMIND_LLM_ENABLED``
   降级为派生语义，用于拦截「以为在跑确定性模式、实际会外发请求」的误配。
4. 所有密钥字段使用 :class:`pydantic.SecretStr`，日志中自动渲染为 ``***``。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from tagmind.core.errors import ConfigError

EnvName = Literal["development", "staging", "production"]
LogFormatName = Literal["json", "console"]
VectorBackendName = Literal["pgvector", "chroma"]
LLMBackendName = Literal["deterministic", "llm_local", "llm_api"]
RoleName = Literal["api", "worker", "beat"]

#: Redis 库号分配（ADR-0006）：``/0`` 应用缓存、``/1`` Celery broker、``/2`` 结果缓存。
REDIS_DB_CACHE = 0
REDIS_DB_BROKER = 1
REDIS_DB_RESULT = 2


def _redis_url_with_db(url: str, db: int) -> str:
    """把 Redis URL 的库号替换为指定值。

    Redis URL 形如 ``redis://[:password@]host:port/db``；这里只改最后的路径段，
    保留用户名、密码与主机端口。输入无法解析时退回「替换最后一个斜杠之后内容」
    的保守做法，绝不抛异常——配置派生失败不应阻断进程启动。
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:  # pragma: no cover - urlsplit 对畸形 URL 极少抛错
        return url
    if not parts.scheme:
        return url
    netloc = parts.netloc
    path = f"/{db}"
    return urlunsplit((parts.scheme, netloc, path, parts.query, parts.fragment))


class Settings(BaseSettings):
    """tagmind 全部运行时配置。

    字段名与 ``.env.example`` 的键名一一对应（剥离 ``TAGMIND_`` 前缀后小写）。
    """

    model_config = SettingsConfigDict(
        env_prefix="TAGMIND_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application --------------------------------------------------------
    env: EnvName = "development"
    log_level: str = "INFO"
    log_format: LogFormatName = "json"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    workers: int = 4
    api_key: SecretStr = SecretStr("")
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]
    role: RoleName = "api"

    # --- PostgreSQL ---------------------------------------------------------
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "tagmind"
    postgres_user: str = "tagmind"
    postgres_password: SecretStr = SecretStr("")
    database_url: str = ""
    db_connect_timeout: int = 3
    db_pool_size: int = 10
    db_max_overflow: int = 10

    # --- Redis --------------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    redis_password: SecretStr = SecretStr("")

    # --- Vector store -------------------------------------------------------
    vector_backend: VectorBackendName = "pgvector"
    vector_dim: int = 1024
    vector_top_k: int = 20

    # --- Labelling model ----------------------------------------------------
    llm_enabled: bool = False
    llm_backend: LLMBackendName = "deterministic"
    llm_api_base: str = "https://open.bigmodel.cn/api/paas/v4"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "glm-4-flash"
    llm_timeout: int = 60
    llm_max_concurrency: int = 8
    llm_temperature: float = 0.0
    llm_max_tokens: int = 2048

    embedding_enabled: bool = False
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024

    # --- Labelling pipeline -------------------------------------------------
    label_batch_size: int = 50
    label_workers: int = 4
    max_content_chars: int = 6000
    conflict_top_k: int = 5
    conflict_sim_threshold: float = 0.82
    conflict_enabled: bool = True

    # --- Quality gate -------------------------------------------------------
    quality_enabled: bool = True
    quality_min_score: float = 6.0
    quality_sensitive_words_path: str = "rules/sensitive_words.txt"
    quality_min_length: int = 20

    # --- Recommendation -----------------------------------------------------
    rec_candidate_size: int = 200
    rec_top_k: int = 20
    rec_cold_start_strategy: str = "trending_latest"
    rec_profile_decay: float = 0.95
    rec_explore_ratio: float = 0.1

    # --- Scheduler ----------------------------------------------------------
    scheduler_enabled: bool = True
    scheduler_timezone: str = "Asia/Shanghai"
    job_label_cron: str = "*/5 * * * *"
    job_profile_cron: str = "0 * * * *"
    job_drift_check_cron: str = "0 3 * * 1"
    job_cleanup_cron: str = "0 4 * * *"

    # --- Observability ------------------------------------------------------
    metrics_enabled: bool = True
    metrics_port: int = 9100
    alert_webhook_url: str = ""
    log_retention_days: int = 14

    # --- Label taxonomy canary (§12.6) --------------------------------------
    label_canary_ratio: float = 0.0
    label_canary_version: str = ""

    # --- Validators ---------------------------------------------------------

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: object) -> object:
        """允许用 JSON 数组或逗号分隔字符串配置 CORS 白名单。

        pydantic-settings 默认对复杂类型强制 JSON 解析，逗号分隔写法会直接
        抛 ``SettingsError``；因此字段标注了 ``NoDecode``，把原始字符串交给
        本校验器统一处理。
        """
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                import json

                try:
                    parsed = json.loads(text)
                except ValueError as exc:
                    raise ValueError(f"TAGMIND_CORS_ORIGINS 不是合法 JSON：{exc}") from exc
                if not isinstance(parsed, list):
                    raise ValueError("TAGMIND_CORS_ORIGINS 必须是 JSON 数组")
                return [str(item) for item in parsed]
            return [item.strip() for item in text.split(",") if item.strip()]
        return value

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        """日志级别统一大写，避免 ``info``/``INFO`` 两种写法行为不一致。"""
        return value.strip().upper()

    def _validate_production_secrets(self) -> None:
        """生产环境必须配置数据库密码，否则启动失败。"""
        if self.env == "production" and not self.postgres_password.get_secret_value():
            raise ConfigError(
                "生产环境必须配置 TAGMIND_POSTGRES_PASSWORD",
                details={"env": self.env, "missing": "TAGMIND_POSTGRES_PASSWORD"},
            )

    def _validate_llm_backend(self) -> None:
        """校验 LLM 后端选择与 ``LLM_ENABLED`` 的派生关系。

        ``LLM_BACKEND`` 是权威；``LLM_ENABLED`` 只能用于「更严」的方向，
        不能用于「更松」的方向——否则 ``LLM_ENABLED=false`` 会被误读成
        「确定性模式」，实际却把正文外发给了云端（架构附录 A.4 的裁决）。
        """
        if not self.llm_enabled and self.llm_backend != "deterministic":
            raise ConfigError(
                "TAGMIND_LLM_ENABLED=false 时 TAGMIND_LLM_BACKEND 必须为 deterministic，"
                "否则会外发内容请求",
                details={
                    "llm_backend": self.llm_backend,
                    "llm_enabled": self.llm_enabled,
                },
            )

    def _validate_canary_ratio(self) -> None:
        """灰度比例是百分比（0–100），越界说明配置写错。"""
        if not 0.0 <= self.label_canary_ratio <= 100.0:
            raise ConfigError(
                "TAGMIND_LABEL_CANARY_RATIO 必须落在 0–100 之间",
                details={"label_canary_ratio": self.label_canary_ratio},
            )

    # --- Derived properties -------------------------------------------------

    @property
    def resolved_database_url(self) -> str:
        """最终使用的数据库 URL。

        ``TAGMIND_DATABASE_URL`` 非空时直接使用；否则由 ``postgres_*`` 五项派生。
        密码做 URL 编码，避免特殊字符破坏连接串。
        """
        if self.database_url:
            return self.database_url
        user = quote(self.postgres_user, safe="")
        password = quote(self.postgres_password.get_secret_value(), safe="")
        netloc = f"{user}:{password}@{self.postgres_host}:{self.postgres_port}"
        return f"postgresql+psycopg://{netloc}/{self.postgres_db}"

    @property
    def redis_broker_url(self) -> str:
        """Celery broker 地址（Redis ``/1``）。"""
        return _redis_url_with_db(self.redis_url, REDIS_DB_BROKER)

    @property
    def redis_cache_url(self) -> str:
        """结果缓存地址（Redis ``/2``）。"""
        return _redis_url_with_db(self.redis_url, REDIS_DB_RESULT)

    @property
    def llm_provider(self) -> str:
        """后端选择 → 供应商身份。

        ``provider`` 表示「后端是谁」，``degraded`` 表示「是否由降级链进入」，
        两者语义不同（歧义 A-08），不可混用。
        """
        return {
            "deterministic": "deterministic",
            "llm_local": "ollama",
            "llm_api": "zhipu",
        }[self.llm_backend]

    @property
    def is_production(self) -> bool:
        """是否生产环境。"""
        return self.env == "production"

    @property
    def canary_enabled(self) -> bool:
        """是否启用了标签体系灰度。"""
        return self.label_canary_ratio > 0 and bool(self.label_canary_version)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程内单例配置。

    用 :func:`functools.lru_cache` 保证「一个进程一份配置」，避免测试或多线程
    下拿到互相矛盾的两份环境快照。
    """
    settings = Settings()
    settings._validate_production_secrets()
    settings._validate_llm_backend()
    settings._validate_canary_ratio()
    if settings.llm_enabled and settings.llm_backend == "deterministic":
        from tagmind.core.logging import get_logger

        get_logger("tagmind.config").warning(
            "llm_enabled 与 llm_backend 冲突，以 llm_backend=deterministic 为准",
            llm_backend=settings.llm_backend,
            llm_enabled=settings.llm_enabled,
        )
    return settings
