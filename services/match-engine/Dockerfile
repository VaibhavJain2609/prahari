# The match-engine image.
#
# Built from the workspace root, not from this directory: the service depends
# on `packages/prahari-common` and `packages/prahari-proto`, which uv resolves
# as workspace members. Building from `services/match-engine/` would put both
# outside the build context and fail at install time.
#
#   docker build -f services/match-engine/Dockerfile .
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
COPY services/match-engine/pyproject.toml services/match-engine/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package prahari-match

COPY packages/prahari-common/ packages/prahari-common/
COPY packages/prahari-proto/ packages/prahari-proto/
COPY services/match-engine/ services/match-engine/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package prahari-match


FROM python:3.12-slim AS runtime

# No compiler, no uv, no build cache. The match engine holds the watchlist in
# memory; the smaller the image, the less there is to audit in
# `docs/SECURITY.md`.
RUN useradd --create-home --uid 10001 prahari
WORKDIR /app

COPY --from=build --chown=prahari:prahari /app/.venv /app/.venv
COPY --from=build --chown=prahari:prahari /app/packages/prahari-common /app/packages/prahari-common
COPY --from=build --chown=prahari:prahari /app/packages/prahari-proto /app/packages/prahari-proto
COPY --from=build --chown=prahari:prahari /app/services/match-engine /app/services/match-engine
COPY --chown=prahari:prahari data/watchlist /app/data/watchlist

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# The gRPC MetadataIngestService is started from inside the FastAPI lifespan
# (app.py) rather than as a second process, so one uvicorn process is the
# entire container -- one thing for a liveness probe and `kubectl logs` to
# watch, not two processes that can silently diverge on whether the pod is up.
USER prahari
EXPOSE 8001 9001

# Shell form, not exec-array: the bind port must follow PRAHARI_MATCH_HTTP_PORT
# (default 8001, matching MatchSettings.http_port) so a profile that changes
# services.matchEngine.port is not silently ignored by the process itself.
CMD ["sh", "-c", "exec uvicorn prahari_match.app:app --host 0.0.0.0 --port ${PRAHARI_MATCH_HTTP_PORT:-8001}"]
