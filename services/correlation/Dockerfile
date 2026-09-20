# The correlation image.
#
# Built from the workspace root, not from this directory: the service depends
# on `packages/prahari-common` and `packages/prahari-proto`, which uv resolves
# as workspace members. Building from `services/correlation/` would put both
# outside the build context and fail at install time.
#
#   docker build -f services/correlation/Dockerfile .
#
# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build

# 3.12 is pinned, not incidental: 3.14 has no PyTorch/Ultralytics wheels, and one
# interpreter across the workspace is worth more than the freedom to differ.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency layer first, and without the source, so that editing a handler
# does not re-resolve the dependency tree on every Tilt rebuild.
COPY pyproject.toml uv.lock ./
COPY packages/prahari-common/pyproject.toml packages/prahari-common/
COPY packages/prahari-proto/pyproject.toml packages/prahari-proto/
COPY services/correlation/pyproject.toml services/correlation/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package prahari-correlation

COPY packages/prahari-common/ packages/prahari-common/
COPY packages/prahari-proto/ packages/prahari-proto/
COPY services/correlation/ services/correlation/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package prahari-correlation


FROM python:3.12-slim AS runtime

# No compiler, no uv, no build cache. Correlation's detection ring buffers are
# still in-memory (Postgres holds the durable copy); the smaller the image, the
# less there is to audit in `docs/SECURITY.md`.
RUN useradd --create-home --uid 10001 prahari
WORKDIR /app

COPY --from=build --chown=prahari:prahari /app/.venv /app/.venv
COPY --from=build --chown=prahari:prahari /app/packages/prahari-common /app/packages/prahari-common
COPY --from=build --chown=prahari:prahari /app/packages/prahari-proto /app/packages/prahari-proto
COPY --from=build --chown=prahari:prahari /app/services/correlation /app/services/correlation

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER prahari
EXPOSE 8002

# Shell form, not exec-array: the bind port must follow
# PRAHARI_CORRELATION_HTTP_PORT (the chart's correlationEnv sets it to
# services.correlation.port; the fallback matches CorrelationSettings.http_port)
# so a profile that sets the env is not silently ignored by the process itself.
CMD ["sh", "-c", "exec uvicorn prahari_correlation.app:app --host 0.0.0.0 --port ${PRAHARI_CORRELATION_HTTP_PORT:-8002}"]
