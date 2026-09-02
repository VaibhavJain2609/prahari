"""Shared fixtures. `MatchSettings` is read once per process via
`@lru_cache` (matching every other service's config style) -- tests that set
`PRAHARI_MATCH_*` env vars must clear that cache first, or a later test would
silently see an earlier test's settings object.
"""

from __future__ import annotations

import pytest

from prahari_match.config import match_settings


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    match_settings.cache_clear()
    yield
    match_settings.cache_clear()
