#!/bin/sh
# ---------------------------------------------------------------------------
# tagmind — 容器入口：按 TAGMIND_ROLE 分派
#
# 单镜像三角色（架构附录 A.4）：
#   api    → uvicorn tagmind.api.app:create_app --factory
#   worker → celery worker
#   beat   → celery beat
#
# 未设置 TAGMIND_ROLE 时默认 api——与既有 Dockerfile 的启动命令一致。
# ---------------------------------------------------------------------------
set -eu

ROLE="${TAGMIND_ROLE:-api}"
API_HOST="${TAGMIND_API_HOST:-0.0.0.0}"
API_PORT="${TAGMIND_API_PORT:-8000}"
LOG_LEVEL="${TAGMIND_LOG_LEVEL:-info}"

case "$ROLE" in
  api)
    exec uvicorn tagmind.api.app:create_app --factory \
      --host "$API_HOST" --port "$API_PORT"
    ;;
  worker)
    exec celery -A tagmind.scheduler.celery_app:celery_app worker \
      --loglevel "$LOG_LEVEL"
    ;;
  beat)
    exec celery -A tagmind.scheduler.celery_app:celery_app beat \
      --loglevel "$LOG_LEVEL"
    ;;
  *)
    echo "未知 TAGMIND_ROLE='$ROLE'，可选值：api | worker | beat" >&2
    exit 1
    ;;
esac
