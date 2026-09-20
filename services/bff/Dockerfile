# The BFF image.
#
# Built from the workspace root, not from this directory: the service depends
# on `packages/prahari-common` and `packages/prahari-proto`, which uv resolves
# as workspace members. Building from `services/bff/` would put both outside
# the build context and fail at install time.
#
#   docker build -f services/bff/Dockerfile .
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
COPY services/bff/pyproject.toml services/bff/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package prahari-bff

COPY packages/prahari-common/ packages/prahari-common/
COPY packages/prahari-proto/ packages/prahari-proto/
COPY services/bff/ services/bff/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package prahari-bff


FROM python:3.12-slim AS runtime

# No compiler, no uv, no build cache. The BFF holds sessions, API keys and the
# audit chain's write path; the smaller the image, the less there is to audit
# in `docs/SECURITY.md`.
RUN useradd --create-home --uid 10001 prahari
WORKDIR /app

COPY --from=build --chown=prahari:prahari /app/.venv /app/.venv
COPY --from=build --chown=prahari:prahari /app/packages/prahari-common /app/packages/prahari-common
COPY --from=build --chown=prahari:prahari /app/packages/prahari-proto /app/packages/prahari-proto
COPY --from=build --chown=prahari:prahari /app/services/bff /app/services/bff

# The audit log is an append-only SQLite file (BFFSettings.audit_db_path). Its
# default is the bare filename resolved against the working directory, which
# would be a root-owned /app the `prahari` user cannot write — so the image
# points it at a writable directory. A chart that mounts a PVC for the audit
# chain should mount it here.
RUN mkdir -p /app/data && chown prahari:prahari /app/data

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PRAHARI_AUDIT_DB_PATH=/app/data/audit.db

USER prahari
EXPOSE 8080

# Port is fixed rather than env-driven: BFFSettings has no port field — the
# bind port is chart-owned (services.bff.port = 8080 in values.yaml) and the
# two must move together.
CMD ["uvicorn", "prahari_bff.app:app", "--host", "0.0.0.0", "--port", "8080"]
