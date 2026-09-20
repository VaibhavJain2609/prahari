"""Shared fixtures. `CorrelationSettings` is read once per process via
`@lru_cache` (matching every other service's config style) -- tests that set
`PRAHARI_CORRELATION_*` env vars must clear that cache first, or a later test
would silently see an earlier test's settings object. Same pattern as
services/match-engine/tests/conftest.py.
"""

from __future__ import annotations

import pytest

from prahari_correlation.config import correlation_settings


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    correlation_settings.cache_clear()
    yield
    correlation_settings.cache_clear()
