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


# --- internal-auth caller map --------------------------------------------------


def test_internal_tokens_defaults_to_empty_map(monkeypatch) -> None:
    # Empty map + empty internal_token is the open/dev posture; the envs the
    # chart writes must not accidentally arm the gate in tests either.
    for name in (
        "PRAHARI_CORRELATION_INTERNAL_TOKENS",
        "PRAHARI_CORRELATION_CALLER_TOKEN_BFF",
    ):
        monkeypatch.delenv(name, raising=False)
    assert CorrelationSettings().internal_tokens == {}


def test_internal_tokens_parses_the_json_form(monkeypatch) -> None:
    monkeypatch.setenv("PRAHARI_CORRELATION_INTERNAL_TOKENS", '{"bff": "tok-bff"}')
    assert CorrelationSettings().internal_tokens == {"bff": "tok-bff"}


def test_internal_tokens_parses_the_comma_form(monkeypatch) -> None:
    monkeypatch.setenv("PRAHARI_CORRELATION_INTERNAL_TOKENS", "bff:tok-bff,inference:tok-inf")
    assert CorrelationSettings().internal_tokens == {
        "bff": "tok-bff",
        "inference": "tok-inf",
    }


def test_caller_token_bff_env_merges_into_the_map(monkeypatch) -> None:
    # The chart's delivery mechanism — one env per accepted caller.
    monkeypatch.setenv("PRAHARI_CORRELATION_CALLER_TOKEN_BFF", "tok-bff")
    assert CorrelationSettings().internal_tokens == {"bff": "tok-bff"}


def test_caller_token_env_beats_the_json_entry_for_the_same_name(monkeypatch) -> None:
    monkeypatch.setenv("PRAHARI_CORRELATION_INTERNAL_TOKENS", '{"bff": "json-val"}')
    monkeypatch.setenv("PRAHARI_CORRELATION_CALLER_TOKEN_BFF", "env-val")
    assert CorrelationSettings().internal_tokens["bff"] == "env-val"
