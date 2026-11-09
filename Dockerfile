# ---------------------------------------------------------------------------
# tagmind — container image
#
# Multi-stage: the builder compiles wheels, the runtime keeps only what the
# service needs. The image runs as a non-root user and never carries secrets:
# every credential is read from the environment at start-up.
# ---------------------------------------------------------------------------

# --- Stage 1: builder -------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# Build tooling for the optional C-extension extras (LightFM). Keeping them in
# the builder stage means the runtime image does not carry a compiler.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Install dependencies first so that the layer is cached across source edits.
COPY pyproject.toml README.md ./
COPY src/tagmind/__init__.py src/tagmind/__init__.py
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip wheel --wheel-dir /wheels \
        "fastapi>=0.110,<1" "uvicorn[standard]>=0.27,<1" \
        "pydantic>=2.6,<3" "pydantic-settings>=2.2,<3" \
        "sqlalchemy>=2.0,<3" "alembic>=1.13,<2" "psycopg[binary]>=3.1,<4" \
        "redis>=5.0,<6" "celery>=5.3,<6" "numpy>=1.26,<3" \
        "httpx>=0.27,<1" "prometheus-client>=0.20,<1" \
        "python-multipart>=0.0.9" "PyYAML>=6.0,<7" "tenacity>=8.2,<9" \
        "structlog>=24.1,<26" "typer>=0.12,<1"

COPY src/ src/
RUN python -m pip wheel --no-deps --wheel-dir /wheels .

# --- Stage 2: runtime -------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    TAGMIND_ENV=production \
    TAGMIND_API_HOST=0.0.0.0 \
    TAGMIND_API_PORT=8000 \
    TAGMIND_ROLE=api

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 tagmind \
    && useradd --system --uid 1001 --gid tagmind --create-home --home-dir /home/tagmind tagmind

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY pyproject.toml README.md ./
RUN python -m pip install --no-cache-dir /wheels/*.whl \
    && rm -rf /wheels

COPY src/ /app/src/
COPY migrations/ /app/migrations/
COPY rules/ /app/rules/
COPY data/ /app/data/
COPY scripts/ /app/scripts/
COPY alembic.ini /app/alembic.ini

RUN mkdir -p /app/data/checkpoints \
    && chmod +x /app/scripts/entrypoint.sh \
    && chown -R tagmind:tagmind /app

USER tagmind

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

# 单镜像三角色，按 TAGMIND_ROLE 分派（api / worker / beat）。
CMD ["/app/scripts/entrypoint.sh"]
