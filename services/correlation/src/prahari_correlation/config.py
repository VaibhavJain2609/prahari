"""Correlation-service configuration. Same style as every other service:
env-only, every value has a working default so a fresh checkout passes the
gate without a `.env`.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class CorrelationSettings(BaseSettings):
    """INVARIANT: every `PRAHARI_CORRELATION_*` name the Helm chart sets must
    exist here as a field, both directions -- see
    `services/match-engine/tests/test_match_settings.py`'s docstring for why
    this repo checks both directions rather than only chart-to-field."""

    model_config = SettingsConfigDict(
        env_prefix="PRAHARI_CORRELATION_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- HTTP surface ----------------------------------------------------

    http_port: int = 8003

    # --- detections bus (DAY3-DESIGN.md §3.1) -----------------------------

    redis_url: str | None = None
    """`None` means the detection consumer never starts -- `/readyz` reports
    this honestly rather than silently returning empty routes forever, same
    reasoning as match-engine's watchlist-empty check."""

    redis_detection_stream_key: str = "prahari:detections"
    """Must match `prahari_match.config.MatchSettings.redis_detection_stream_key`
    -- both are the same code-level contract with the same default, checked
    only by convention today (each is chart-exposed under its own service
    prefix), not by a shared test."""

    # --- detection store (DAY3-DESIGN.md §3.1) -----------------------------

    max_detections_per_plate: int = 500
    """Bounded ring buffer per plate key -- a laptop-scale demo store, not a
    database. A vehicle seen thousands of times (a taxi, a delivery van) must
    not grow one plate's history without limit."""

    max_tracked_plates: int = 50_000
    """LRU cap on distinct plate keys held in memory, same bound-the-process
    reasoning as `MatchSettings.dedup_max_entries`."""

    # --- feasibility gating (DAY3-DESIGN.md §3.2) ---------------------------

    max_speed_kmh: float = 120.0
    """A hop between two cameras is rejected when the great-circle distance
    over the elapsed time implies an average speed above this. Deliberately
    a speed envelope, not a hard distance cap -- a 200 km hop over 3 hours is
    plausible, the same 200 km over 3 minutes is not."""

    camera_location_cache_ttl_s: float = 300.0
    """How long a camera's `GeoPoint`, fetched from the registry, is cached
    before being re-fetched. The registry is the source of truth for camera
    location (CLAUDE.md); this is a read-through cache, not a second copy --
    a camera relocated mid-demo is stale here for at most this long."""

    # --- appearance gap bridging (DAY3-DESIGN.md §3.3) ----------------------

    appearance_similarity_threshold: float = 0.85
    """Cosine similarity floor for bridging two plate-confirmed segments
    across a plate-unreadable detection. Tuned against
    `tests/test_bridging.py`'s accuracy cases, not picked from theory."""

    # --- registry client -----------------------------------------------------

    registry_base_url: str = "http://registry:8000"
    registry_timeout_s: float = 5.0


@lru_cache(maxsize=1)
def correlation_settings() -> CorrelationSettings:
    return CorrelationSettings()
