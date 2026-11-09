"""Celery 应用：队列路由、DLQ 落地与重试策略。

架构契约（§9.2 / §9.4 / ADR-0009）：

- **幂等的最终保证在 DB 唯一约束**，Redis checkpoint 只做加速不做保证。
- 失败任务进 DLQ，保留原始 payload + 失败原因 + 重试次数，可重放。
- 重试用指数退避；4xx 类不可重试错误直接进 DLQ。

阶段 0 只搭骨架：应用、队列路由与重试策略。任务本体在阶段 1/3 实现。
"""

from __future__ import annotations

from celery import Celery
from celery.signals import task_failure

from tagmind.config.settings import get_settings
from tagmind.core.logging import configure_logging, get_logger

__all__ = ["celery_app"]

logger = get_logger("tagmind.scheduler")

settings = get_settings()
configure_logging(level=settings.log_level, fmt=settings.log_format)

celery_app = Celery(
    "tagmind",
    broker=settings.redis_broker_url,
    backend=settings.redis_cache_url,
    include=["tagmind.scheduler.tasks"],
)

celery_app.conf.update(
    # 任务路由：打标任务独立队列，避免与画像重建互相饿死。
    task_routes={
        "tagmind.label.*": {"queue": "label"},
        "tagmind.profile.*": {"queue": "profile"},
        "tagmind.maintenance.*": {"queue": "maintenance"},
    },
    task_default_queue="default",
    # 幂等：同一个 idempotency_key 的重复提交必须被识别，
    # 因此任务使用「不带自动重试的确定性命名」策略由 DB 唯一约束兜底。
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=1800,
    task_soft_time_limit=1500,
    timezone=settings.scheduler_timezone,
    enable_utc=True,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
    # Redis 断连后的重连策略必须显式给出：默认值是无退避的立即重连，
    # 在 broker 短暂不可用时会变成连接风暴，把 Redis 与 worker 一起压垮。
    broker_connection_max_retries=10,
    broker_transport_options={
        "max_retries": 10,
        "interval_start": 0.5,
        "interval_step": 2.0,
        "interval_max": 30.0,
    },
)


@task_failure.connect
def _on_task_failure(
    sender: object | None = None,
    task_id: str | None = None,
    exception: BaseException | None = None,
    args: tuple[object, ...] | None = None,
    kwargs: dict[str, object] | None = None,
    traceback: object | None = None,
    **extra: object,
) -> None:
    """任务失败时记录结构化日志。

    DLQ 的持久化落库由阶段 1 的 ``scheduler/tasks/labeling.py`` 负责
    （需要 ``content`` 表与幂等键支撑）；这里只保证日志里有完整上下文。

    ``**extra`` 是必需的：Celery 的信号会携带本函数未显式声明的关键字参数
    （例如 ``retries``、``einfo``），缺少它时 Celery 会在派发信号时抛出
    ``ValueError: Signal receiver must accept keyword arguments``，
    反而掩盖掉真正的任务失败。
    """
    logger.error(
        "celery.task.failed",
        task_id=task_id,
        task=getattr(sender, "name", None),
        error=str(exception) if exception else None,
        args=args,
        retries=extra.get("retries"),
    )
