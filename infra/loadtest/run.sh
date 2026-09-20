#!/usr/bin/env bash
# Thin entry point: run the harness inside the uv workspace environment.
# All arguments pass through to `python -m prahari_loadtest`.
set -euo pipefail
cd "$(dirname "$0")"
exec uv run python -m prahari_loadtest "$@"
