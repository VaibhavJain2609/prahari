"""Match-engine configuration. Environment only, same style as the other
services: every value has a working default for `make up`, so a fresh
checkout needs no `.env` to pass the Day 2 gate.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class MatchSettings(BaseSettings):
    """INVARIANT: every `PRAHARI_MATCH_*` name the Helm chart sets must exist
    here as a field. `tests/test_match_settings.py` parses `_helpers.tpl`'s
    `matchEngineEnv` block and asserts exactly that, both directions -- unlike
    `DetectorSettings`, which only asserts chart-to-field (see its docstring):
    M3 found the reverse gap here mattered in practice (`redis_url` silent
    everywhere), so a field the chart could plausibly want to set must be
    either chart-exposed or named in that test's `_DELIBERATELY_INTERNAL` set,
    with a reason. A knob the chart writes and the code never reads is a
    profile switch that silently does not switch, which is worse than no
    switch: the profile looks applied and is not.
    """

    model_config = SettingsConfigDict(
        env_prefix="PRAHARI_MATCH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- gRPC surface (MetadataIngestService, the worker link) ---------------

    grpc_host: str = "0.0.0.0"
    grpc_port: int = 9001
    grpc_max_workers: int = 10

    # --- HTTP surface --------------------------------------------------------

    http_port: int = 8001
    """Read by nothing in-process -- `uvicorn`'s bind port is the Dockerfile
    CMD's `--port`, sourced from this same env var with the same default so
    the two cannot drift. Still a real settings field: FastAPI/other code
    that needs to know its own port (e.g. building a self-referential URL)
    reads it from here, not by re-parsing argv."""

    # --- watchlist -------------------------------------------------------

    watchlist_dir: str = "data/watchlist"
    """Directory of `.json`/`.csv` watchlist snapshots, loaded on startup and
    on `/api/v1/watchlist/reload`. Not a database -- the watchlist is small
    (thousands of entries, not millions) and reloading from disk is fast
    enough that a database's operational cost is not worth carrying for it."""

    # --- Bloom filter (stage 1) ------------------------------------------

    bloom_expected_entries: int = 20_000
    """Sizing input, not a hard cap. The filter is built as
    `max(len(watchlist.bloom_keys()), bloom_expected_entries)`, so a smaller
    real watchlist still gets a filter sized for the estate-scale figure this
    hackathon targets, and `current_false_positive_rate` stays honest either
    way."""

    bloom_target_fp_rate: float = 0.001
    """`bloom_keys()` seeds the filter with each skeleton's deletion variants
    too (~11x the entry count for a 10-character plate), and `matcher.match`
    probes it with up to `1 + len(plate)` keys per detection. Both amplify
    the effective rejection failure rate over this per-key target, so it is
    sized an order of magnitude below the ~1% the funnel is meant to show at
    `/readyz` -- not the rate itself."""

    # --- scoring (stage 3) -------------------------------------------------

    score_decay: float = 3.0
    """Divisor in `exp(-weighted_distance / decay)`. Larger tolerates more
    accumulated edit cost before the score collapses; tuned against the
    accuracy-table tests in `tests/test_matcher.py`, not picked from theory."""

    confirmed_score: float = 0.85
    """"act on it" -- DAY2-DESIGN §7.2 / `ConfidenceBand.CONFIRMED`."""

    probable_score: float = 0.55
    """"likely, needs corroboration"."""

    weak_score: float = 0.25
    """"worth a look". Below this the candidate is discarded entirely rather
    than surfaced as an alert -- an unbounded WEAK band would make every
    plausible-looking non-match an alert, which is worse than no match."""

    max_candidates: int = 64
    """Safety cap on candidates scored per detection, on top of the bounded
    generation in `matcher.py`. Candidate generation is already O(len(plate)),
    not O(watchlist size); this exists so a pathological watchlist (many
    entries sharing one skeleton bucket) cannot turn one detection into an
    unbounded amount of scoring work."""

    # --- dedup -------------------------------------------------------------

    dedup_bucket_s: float = 8.0
    """DAY2-DESIGN §7.3: a vehicle in frame for 8 s at 3 fps is one alert, not
    24. Matches the example dwell time; a camera with a longer capture zone
    (a slow approach, a toll queue) will want this larger."""

    dedup_max_entries: int = 100_000
    """LRU cap on the dedup table. Bounded so a long-running process does not
    grow this without limit -- every distinct (camera, plate, bucket) ever seen
    would otherwise stay resident forever."""

    # --- alert fan-out -------------------------------------------------------

    redis_url: str | None = None
    """`None` means "no bus fan-out" -- alerts are still built, scored and
    queryable via `/api/v1/alerts`, just not published onto Redis Streams.
    Deliberately optional: the accuracy tests, and a laptop run before Redis
    is wired into `make up`, must not require it."""

    redis_stream_key: str = "prahari:alerts"

    recent_alerts_size: int = 500
    """Bounded in-memory ring buffer backing `/api/v1/alerts` -- a debug/admin
    surface, not the system of record. That is the bus, when configured."""


@lru_cache(maxsize=1)
def match_settings() -> MatchSettings:
    return MatchSettings()
