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

    grpc_max_concurrent_streams: int = 100
    """`grpc.max_concurrent_streams` server option: caps HTTP/2 streams per
    connection so one misbehaving worker cannot open unbounded streams. The
    handler pool is `grpc_max_workers` (10) threads; a worker holding a stream
    open is cheap until it sends, so the cap sits well above the worker
    count — it bounds memory, not concurrency."""

    grpc_max_receive_message_bytes: int = 4 * 1024 * 1024
    """`grpc.max_receive_message_length` server option. A `VehicleDetection` is
    hundreds of bytes; 4 MiB is generous headroom for a coalesced or padded
    message while still refusing the genuinely malformed."""

    max_raw_text_chars: int = 64
    """Upper bound on `PlateReading.raw_text` accepted at the gRPC boundary.
    No real plate is anywhere near 64 characters; beyond it the reading is a
    bug or an attack, and `match()` re-normalises it into scoring work for
    nothing. Rejected messages count in `IngestAck.rejected`."""

    max_char_confidences: int = 64
    """Upper bound on `PlateReading.char_confidence` list length. The list is
    index-aligned with `raw_text` (bounded by `max_raw_text_chars`), so a
    longer list is malformed input, not signal."""

    # --- HTTP surface --------------------------------------------------------

    http_port: int = 8001
    """Read by nothing in-process -- `uvicorn`'s bind port is the Dockerfile
    CMD's `--port`, sourced from this same env var with the same default so
    the two cannot drift. Still a real settings field: FastAPI/other code
    that needs to know its own port (e.g. building a self-referential URL)
    reads it from here, not by re-parsing argv."""

    # --- access --------------------------------------------------------------

    internal_token: str = ""
    """Required as `X-Internal-Token` on every `/api/*` request and as
    `x-internal-token` gRPC metadata on `MetadataIngestService`, when set --
    the same gate the registry runs (`RegistrySettings.internal_token`), on
    both surfaces this service exposes. The watchlist and its alerts are
    exactly what access control exists for, and an unauthenticated
    `MetadataIngestService` lets anything cluster-reachable inject detections
    into the evidence trail. Empty disables both gates -- the local/dev
    default; the chart arms it per profile."""

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

    redis_socket_timeout_s: float = 5.0
    """`socket_timeout` for `redis.Redis.from_url`. Without it a hung Redis
    wedges a gRPC handler thread on `XADD` forever; with 10 handler threads a
    dead Redis would take the whole ingest link down with it. A timed-out
    publish logs and drops instead (see alerts.py/detections.py)."""

    redis_socket_connect_timeout_s: float = 2.0
    """`socket_connect_timeout` for `redis.Redis.from_url` -- the connect path
    gets a tighter bound than steady-state reads so a dead Redis fails fast
    rather than queuing behind TCP retries."""

    redis_stream_key: str = "prahari:alerts"

    alert_stream_maxlen: int = 50_000
    """`XADD ... MAXLEN ~` cap on the alerts stream — previously a bare XADD,
    so a long-running demo grew `prahari:alerts` without bound. Alerts are the
    lower-rate of the two streams this service writes (watchlist hits only,
    already deduped), so the cap sits a quarter of `detection_stream_maxlen`."""

    recent_alerts_size: int = 500
    """Bounded in-memory ring buffer backing `/api/v1/alerts` -- a debug/admin
    surface, not the system of record. That is the bus, when configured."""

    # --- detections bus (DAY3-DESIGN.md §2) -----------------------------------

    redis_detection_stream_key: str = "prahari:detections"
    """Every `VehicleDetection`, watchlist hit or not -- `prahari:alerts` only ever
    carried matches, which left `services/correlation` nothing to reconstruct a route
    from for a plate that was never on the watchlist. Reuses `redis_url`: one Redis,
    two streams, same "no bus fan-out when unset" rule as alerts."""

    detection_stream_maxlen: int = 200_000
    """`XADD ... MAXLEN ~` cap. This stream carries every detection rather than only
    watchlist hits, so it is the higher-rate of the two and the one actually at risk of
    growing without bound on a long-running demo."""


@lru_cache(maxsize=1)
def match_settings() -> MatchSettings:
    return MatchSettings()
