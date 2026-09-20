"""config.py: the env contract the chart writes against.

The parity test at the repo root asserts chart -> field; these assert the
field -> env direction for the names the durability work added, plus the
unset-means-degraded defaults the rest of the service relies on.
"""

from __future__ import annotations

from prahari_correlation.config import CorrelationSettings


def test_database_url_defaults_to_none_memory_only(monkeypatch) -> None:
    monkeypatch.delenv("PRAHARI_CORRELATION_DATABASE_URL", raising=False)
    settings = CorrelationSettings()
    assert settings.database_url is None


def test_database_url_reads_the_service_prefixed_env(monkeypatch) -> None:
    # The chart emits PRAHARI_CORRELATION_DATABASE_URL — the env_prefix is
    # PRAHARI_CORRELATION_, so a bare PRAHARI_DATABASE_URL would NOT be read
    # (that name belongs to registry/bff, and this test pins the boundary).
    monkeypatch.setenv(
        "PRAHARI_CORRELATION_DATABASE_URL",
        "postgresql://prahari:x@prahari-postgres:5432/prahari",
    )
    settings = CorrelationSettings()
    assert settings.database_url == "postgresql://prahari:x@prahari-postgres:5432/prahari"


def test_route_history_bound_has_a_default(monkeypatch) -> None:
    monkeypatch.delenv("PRAHARI_CORRELATION_ROUTE_HISTORY_MAX_SIGHTINGS", raising=False)
    settings = CorrelationSettings()
    assert settings.route_history_max_sightings > 0
