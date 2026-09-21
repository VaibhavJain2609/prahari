"""The match engine's HTTP surface: liveness/readiness, watchlist admin, and a
debug/admin view of recent alerts.

REST/JSON, same split as every other service in this codebase: the high-rate
worker link is gRPC (`grpc_server.py`), everything low-rate and human- or
BFF-facing is JSON. The gRPC server is started and stopped alongside this
app's lifespan so `uvicorn` remains the single process a Helm liveness probe
needs to watch.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse
from prahari_common.internal_auth import caller_accepted, gate_posture, provided_token
from pydantic import BaseModel

from .alert_store import (
    AlertStore,
    AlertStorePublisher,
    MemoryAlertStore,
    PostgresAlertStore,
)
from .alerts import AlertPublisher, FanOutPublisher, RedisStreamPublisher
from .bloom import BloomFilter
from .config import ACCEPTED_CALLERS, MatchSettings, match_settings
from .db import apply_migrations, create_pool
from .dedup import Deduper
from .detections import DetectionPublisher, NullDetectionPublisher, RedisDetectionPublisher
from .grpc_server import serve
from .matcher import WatchlistStore
from .metrics import ALERT_PERSIST_FAILURES, BLOOM_FP_RATE, METRICS, WATCHLIST_ENTRIES
from .watchlist import Watchlist

__all__ = ["app"]

log = logging.getLogger(__name__)

# Grace period for in-flight worker streams to finish before the gRPC server
# is torn down. Long enough to drain a batch, short enough not to stall a pod
# eviction past what Kubernetes will tolerate.
_GRPC_STOP_GRACE_S = 5.0


def _load_watchlist(settings: MatchSettings) -> Watchlist:
    return Watchlist.load_dir(Path(settings.watchlist_dir))


def _build_bloom(watchlist: Watchlist, settings: MatchSettings) -> BloomFilter:
    """Sized off `max(len(watchlist.bloom_keys()), bloom_expected_entries)` --
    see `MatchSettings.bloom_expected_entries` for why a small real watchlist
    still gets an estate-scale-sized filter, and `Watchlist.bloom_keys` for
    why the key count is ~11x the entry count, not 1x."""
    keys = list(watchlist.bloom_keys())
    bloom = BloomFilter(
        expected_items=max(len(keys), settings.bloom_expected_entries),
        target_fp_rate=settings.bloom_target_fp_rate,
    )
    for key in keys:
        bloom.add(key)
    return bloom


def _record_watchlist_gauges(store: WatchlistStore) -> None:
    """Refresh the `/metrics` gauges that describe the live watchlist --
    called at startup and after every reload so a stale filter or a dropped
    watchlist shows up as a number, not a surprise."""
    snapshot = store.snapshot()
    METRICS.set_gauge(WATCHLIST_ENTRIES, len(snapshot.watchlist))
    METRICS.set_gauge(BLOOM_FP_RATE, snapshot.bloom.current_false_positive_rate)


def _watchlist_summary(store: WatchlistStore) -> dict:
    # One snapshot, not four separate `store.watchlist` / `store.bloom`
    # reads -- a reload landing mid-call must not produce a summary mixing
    # the old watchlist's entry count with the new bloom filter's stats.
    snapshot = store.snapshot()
    return {
        "entries": len(snapshot.watchlist),
        "skeleton_buckets": snapshot.watchlist.bucket_count(),
        "bloom_size_bits": snapshot.bloom.size_bits,
        "bloom_hash_count": snapshot.bloom.hash_count,
        "bloom_false_positive_rate": snapshot.bloom.current_false_positive_rate,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = match_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    watchlist = _load_watchlist(settings)
    bloom = _build_bloom(watchlist, settings)
    store = WatchlistStore(watchlist, bloom)
    deduper = Deduper(bucket_s=settings.dedup_bucket_s, max_entries=settings.dedup_max_entries)

    # Alert history. Postgres (when `database_url` is set) is the system of
    # record and is persisted to *ahead of* the Redis XADD in the fan-out —
    # submitted, not awaited; see alert_store.py's module docstring for the
    # ordering trade-off. The in-memory store is the fallback when no database
    # is configured or Postgres cannot be reached/migrated at startup: a
    # history outage must not take the live alert relay down with it.
    memory_alerts = MemoryAlertStore(max_size=settings.recent_alerts_size)
    alert_store: AlertStore = memory_alerts
    publishers: list[AlertPublisher] = []
    pool = None
    if settings.database_url:
        try:
            pool = await create_pool(settings)
            applied = await apply_migrations(pool)
            if applied:
                log.info("applied migrations: %s", ", ".join(applied))
            alert_store = PostgresAlertStore(pool)
            publishers.append(AlertStorePublisher(alert_store, asyncio.get_running_loop()))
        except Exception:
            METRICS.inc(ALERT_PERSIST_FAILURES)
            log.exception(
                "PRAHARI_MATCH_DATABASE_URL is set but Postgres could not be "
                "reached or migrated; alert history degrades to the in-memory store"
            )
            if pool is not None:
                await pool.close()
                pool = None
    if alert_store is memory_alerts:
        publishers.append(memory_alerts)
        if not settings.database_url:
            log.warning(
                "PRAHARI_MATCH_DATABASE_URL not set; alert history is in-memory "
                "only and lost on restart"
            )
    if settings.redis_url:
        publishers.append(
            RedisStreamPublisher(
                settings.redis_url,
                settings.redis_stream_key,
                settings.alert_stream_maxlen,
                socket_timeout_s=settings.redis_socket_timeout_s,
                socket_connect_timeout_s=settings.redis_socket_connect_timeout_s,
            )
        )
    else:
        log.warning("PRAHARI_MATCH_REDIS_URL not set; alerts fan out only to /api/v1/alerts")
    posture = gate_posture(settings.internal_token, settings.internal_tokens)
    if posture == "open":
        log.warning(
            "internal auth disabled: neither PRAHARI_MATCH_INTERNAL_TOKEN nor a "
            "caller-token map is set, so /api/* and MetadataIngestService are "
            "reachable by anything that can reach this pod"
        )
    else:
        log.info("internal auth posture: %s", posture)
    fan_out: AlertPublisher = FanOutPublisher(publishers) if len(publishers) > 1 else publishers[0]

    detection_publisher: DetectionPublisher
    if settings.redis_url:
        detection_publisher = RedisDetectionPublisher(
            settings.redis_url,
            settings.redis_detection_stream_key,
            settings.detection_stream_maxlen,
            socket_timeout_s=settings.redis_socket_timeout_s,
            socket_connect_timeout_s=settings.redis_socket_connect_timeout_s,
        )
    else:
        detection_publisher = NullDetectionPublisher()

    app.state.settings = settings
    app.state.store = store
    app.state.deduper = deduper
    app.state.alert_store = alert_store
    app.state.alert_pool = pool
    app.state.persistence = "postgres" if pool is not None else "memory"
    app.state.publisher = fan_out
    _record_watchlist_gauges(store)

    grpc_server = serve(store, deduper, fan_out, settings, detection_publisher=detection_publisher)
    app.state.grpc_server = grpc_server
    try:
        yield
    finally:
        grpc_server.stop(_GRPC_STOP_GRACE_S)
        if pool is not None:
            await pool.close()


app = FastAPI(
    title="PRAHARI match engine",
    version="0.1.0",
    summary="Confusion-aware watchlist matching and alert fan-out",
    lifespan=lifespan,
)


@app.middleware("http")
async def require_internal_token(request: Request, call_next):
    """Same gate as the registry's `require_internal_token`, on this service's
    HTTP surface — watchlist admin and the alerts debug view are exactly what
    the token exists to keep cluster-internal. The gRPC surface is gated
    separately, by `InternalTokenInterceptor` in `grpc_server.py`.

    `/healthz` and `/readyz` are exempt — a probe carries no data and must not
    depend on a secret being wired correctly to answer. `/metrics` is exempt
    too: Prometheus cannot hold a bearer credential for a pod scrape (and the
    observability NetworkPolicy restricts the port to the monitoring
    namespace on any CNI that enforces it). With neither `internal_token` nor
    `internal_tokens` configured the gate is off entirely (`caller_accepted`);
    with `internal_tokens` set it runs isolated — a token must resolve to an
    accepted caller (`inference`, `bff`), not merely match a shared value."""
    # Tests that build the app without lifespan never set app.state.settings —
    # an absent settings object means an absent token, which is gate-off anyway.
    settings: MatchSettings | None = getattr(request.app.state, "settings", None)
    armed = bool(settings and (settings.internal_token or settings.internal_tokens))
    if armed and request.url.path not in ("/healthz", "/readyz", "/metrics"):
        if not caller_accepted(
            provided_token(request.headers),
            internal_token=settings.internal_token,
            caller_tokens=settings.internal_tokens,
            accepted_callers=ACCEPTED_CALLERS,
        ):
            return Response(
                status_code=status.HTTP_401_UNAUTHORIZED, content="internal token required"
            )
    return await call_next(request)


# --- dependencies ------------------------------------------------------------


def get_store(request: Request) -> WatchlistStore:
    return request.app.state.store


def get_settings(request: Request) -> MatchSettings:
    return request.app.state.settings


def get_alert_store(request: Request) -> AlertStore:
    # Day-2 gate tests build the app without running lifespan; a missing
    # state attribute should mean "memory store", not AttributeError.
    store = getattr(request.app.state, "alert_store", None)
    if store is None:
        store = MemoryAlertStore(max_size=500)
        request.app.state.alert_store = store
    return store


StoreDep = Annotated[WatchlistStore, Depends(get_store)]
SettingsDep = Annotated[MatchSettings, Depends(get_settings)]
AlertStoreDep = Annotated[AlertStore, Depends(get_alert_store)]


# --- probes ------------------------------------------------------------------


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict:
    """Liveness. Deliberately does NOT touch the watchlist or the gRPC server --
    see registry's `/healthz` for why a liveness probe must never depend on
    the thing that would need a restart to fix (a restart here would also drop
    every worker's open gRPC stream, which a mere watchlist-reload should
    never trigger)."""
    return {"status": "ok", "service": "match-engine"}


@app.get("/readyz", tags=["ops"])
async def readyz(request: Request, store: StoreDep, response: Response) -> dict:
    """Readiness must report whether the watchlist actually loaded -- an empty
    watchlist means every detection is guaranteed to miss, which is a silent
    total failure indistinguishable from "nothing is on the watchlist today"
    unless this endpoint says so explicitly.

    `persistence` reports where alert history is being written: "postgres",
    "memory" (database_url unset or startup connect failed), or "degraded"
    (the configured pool can no longer serve a query). It is reported, not
    gated: a 503 here would restart pods while live alerting still works --
    history rides a different sink than the relay on purpose.

    `internal_auth` reports the token gate's posture -- `isolated` |
    `shared` | `open` -- so the credential model a pod is enforcing is
    visible without reading its env.
    """
    settings: MatchSettings | None = getattr(request.app.state, "settings", None)
    posture = gate_posture(
        settings.internal_token if settings else "",
        settings.internal_tokens if settings else {},
    )
    summary = _watchlist_summary(store)
    if summary["entries"] == 0:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "unavailable",
            "reason": "watchlist has 0 entries",
            "internal_auth": posture,
            **summary,
        }
    persistence = getattr(request.app.state, "persistence", "memory")
    if persistence == "postgres":
        try:
            await request.app.state.alert_pool.fetchval("SELECT 1")
        except Exception as exc:  # noqa: BLE001 - report the failure mode, any failure mode
            persistence = f"degraded ({type(exc).__name__})"
    return {
        "status": "ready",
        "persistence": persistence,
        "internal_auth": posture,
        **summary,
    }


@app.get("/metrics", tags=["ops"], response_class=PlainTextResponse)
async def metrics() -> str:
    """Prometheus text exposition of the process-local counters and gauges --
    ingest accept/reject, funnel rates, dedup suppression, publish failures,
    watchlist size, bloom fp rate, in-flight gRPC streams. No prometheus
    client dep: the service needs a dozen numbers, not a registry."""
    return METRICS.render()


# --- watchlist admin -----------------------------------------------------------


@app.get("/api/v1/watchlist/summary", tags=["watchlist"])
async def watchlist_summary(store: StoreDep) -> dict:
    return _watchlist_summary(store)


@app.post("/api/v1/watchlist/reload", tags=["watchlist"])
def watchlist_reload(request: Request, settings: SettingsDep) -> dict:
    """Reload from `settings.watchlist_dir` and swap it in atomically.

    Rebuilds a fresh `Watchlist` and `BloomFilter` off to the side and only
    then calls `store.replace`, which swaps both into a single new
    `WatchlistSnapshot` in one attribute assignment (P5) -- an in-progress
    gRPC match reading via `store.snapshot()` sees either the whole old pair
    or the whole new pair, never bloom from one and the index from the other.

    P6: plain `def`, not `async def` -- `_load_watchlist` walks the watchlist
    directory and parses every JSON/CSV file, and `_build_bloom` hashes every
    skeleton. On a 20k-entry watchlist that is real time on a disk, and an
    `async def` runs it straight on the event loop, stalling `/healthz`,
    `/readyz` and every other request for the duration. FastAPI runs a sync
    path operation in its threadpool instead.
    """
    store: WatchlistStore = request.app.state.store
    watchlist = _load_watchlist(settings)
    bloom = _build_bloom(watchlist, settings)
    store.replace(watchlist, bloom)
    _record_watchlist_gauges(store)
    return {"status": "reloaded", **_watchlist_summary(store)}


# --- alerts ------------------------------------------------------------------


@app.get("/api/v1/alerts", tags=["alerts"])
async def list_alerts(
    store: AlertStoreDep,
    since: Annotated[
        datetime | None,
        Query(description="ISO-8601; only alerts occurring at or after this instant"),
    ] = None,
    camera_id: str | None = None,
    plate: Annotated[
        str | None,
        Query(description="observed or matched watchlist plate, exact match"),
    ] = None,
    acknowledged: bool | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[dict]:
    """Alert history, newest first. Reads Postgres when
    `PRAHARI_MATCH_DATABASE_URL` is configured (the system of record), else
    the bounded in-memory store -- same response shape either way: the full
    `Alert` payload plus `id`, `occurred_at`, `acknowledged_at`,
    `acknowledged_by`."""
    records = await store.list(
        since=since,
        camera_id=camera_id,
        plate=plate,
        acknowledged=acknowledged,
        limit=limit,
    )
    return [record.to_dict() for record in records]


@app.get("/api/v1/alerts/{alert_id}", tags=["alerts"])
async def get_alert(alert_id: str, store: AlertStoreDep) -> dict:
    record = await store.get(alert_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no alert {alert_id}")
    return record.to_dict()


class _AckBody(BaseModel):
    """Optional JSON body for the ack endpoint. `by` is who acknowledged --
    the BFF passes the principal's subject; a bare curl can send the
    `X-Ack-By` header instead. Both absent records "unknown" rather than
    inventing an actor."""

    by: str | None = None


@app.post("/api/v1/alerts/{alert_id}/ack", tags=["alerts"])
async def acknowledge_alert(
    alert_id: str,
    store: AlertStoreDep,
    payload: _AckBody | None = None,
    x_ack_by: Annotated[str | None, Header()] = None,
) -> dict:
    """Acknowledge an alert -- the only lifecycle transition (audit decision:
    no assignment workflow). Idempotent and first-write-wins: re-acking
    returns the record unchanged rather than overwriting the original actor
    and timestamp, since `acknowledged_by` is the audit trail of who cleared
    it."""
    by = (payload.by if payload and payload.by else x_ack_by) or "unknown"
    record = await store.acknowledge(alert_id, by)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no alert {alert_id}")
    return {"status": "acknowledged", **record.to_dict()}
