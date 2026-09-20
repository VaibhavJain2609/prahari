"""Shared fixtures. `MatchSettings` is read once per process via
`@lru_cache` (matching every other service's config style) -- tests that set
`PRAHARI_MATCH_*` env vars must clear that cache first, or a later test would
silently see an earlier test's settings object.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from prahari_match.config import match_settings


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    match_settings.cache_clear()
    yield
    match_settings.cache_clear()


class FakeRedis:
    """Stands in for `redis.Redis` so tests exercise the Redis-backed
    publishers without a server. `from_url` records the kwargs it was called
    with (that is where the socket timeouts live); `xadd` records each call
    and raises `TimeoutError` while `failures_remaining` > 0, simulating a
    hung Redis under a socket timeout."""

    from_url_calls: list[dict] = []
    xadd_calls: list[dict] = []
    failures_remaining: int = 0

    @classmethod
    def reset(cls) -> None:
        cls.from_url_calls = []
        cls.xadd_calls = []
        cls.failures_remaining = 0

    @classmethod
    def from_url(cls, url: str, **kwargs):
        cls.from_url_calls.append({"url": url, **kwargs})
        return cls()

    def xadd(self, stream, fields, **kwargs):  # noqa: ANN001, ANN202
        type(self).xadd_calls.append({"stream": stream, "fields": fields, **kwargs})
        if type(self).failures_remaining:
            type(self).failures_remaining -= 1
            raise TimeoutError("simulated redis socket timeout")
        return "1-0"


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch):
    """Installs `FakeRedis` as the `redis` module for one test. Both Redis
    publishers import `redis` lazily inside `_client_or_connect`, so patching
    `sys.modules` is all it takes."""
    FakeRedis.reset()
    monkeypatch.setitem(sys.modules, "redis", SimpleNamespace(Redis=FakeRedis))
    return FakeRedis
