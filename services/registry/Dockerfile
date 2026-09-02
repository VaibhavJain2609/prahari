# The registry image.
#
# Built from the workspace root, not from this directory: the service depends on
# `packages/prahari-common`, which uv resolves as a workspace member. Building
# from `services/registry/` would put that package outside the build context and
# fail at install time.
#
#   docker build -f services/registry/Dockerfile .
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

# Dependency layer first, and without the source, so that editing a handler does
# not re-resolve the dependency tree on every Tilt rebuild.
COPY pyproject.toml uv.lock ./
COPY packages/prahari-common/pyproject.toml packages/prahari-common/
COPY services/registry/pyproject.toml services/registry/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package prahari-registry

COPY packages/prahari-common/ packages/prahari-common/
COPY services/registry/ services/registry/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package prahari-registry


FROM python:3.12-slim AS runtime

# No compiler, no uv, no build cache. The registry holds camera locations and
# gateway credentials in memory; the smaller the image, the less there is to
# audit in `docs/SECURITY.md`.
RUN useradd --create-home --uid 10001 prahari
WORKDIR /app

COPY --from=build --chown=prahari:prahari /app/.venv /app/.venv
COPY --from=build --chown=prahari:prahari /app/packages/prahari-common /app/packages/prahari-common
COPY --from=build --chown=prahari:prahari /app/services/registry /app/services/registry

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# The service applies its own migrations on startup under an advisory lock, so a
# fresh `helm install` needs no migration Job and no `kubectl exec`. Migrations
# live beside the package in the source tree and are resolved from either
# location — see db.py::_resolve_migrations_dir.
USER prahari
EXPOSE 8000

# One worker. Scaling is horizontal (replicas), not per-pod: the sync advisory
# lock and the retention loop both assume a process is a process.
CMD ["uvicorn", "prahari_registry.app:app", "--host", "0.0.0.0", "--port", "8000"]
