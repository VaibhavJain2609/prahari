"""The match engine's HTTP surface: liveness/readiness, watchlist admin, and a
debug/admin view of recent alerts.

REST/JSON, same split as every other service in this codebase: the high-rate
worker link is gRPC (`grpc_server.py`), everything low-rate and human- or
BFF-facing is JSON. The gRPC server is started and stopped alongside this
app's lifespan so `uvicorn` remains the single process a Helm liveness probe
needs to watch.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from google.protobuf.json_format import MessageToDict

from .alerts import FanOutPublisher, RecentAlertsPublisher, RedisStreamPublisher
from .bloom import BloomFilter
from .config import MatchSettings, match_settings
from .dedup import Deduper
from .detections import DetectionPublisher, NullDetectionPublisher, RedisDetectionPublisher
from .grpc_server import serve
from .matcher import WatchlistStore
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

    recent = RecentAlertsPublisher(max_size=settings.recent_alerts_size)
    publishers = [recent]
    if settings.redis_url:
        publishers.append(RedisStreamPublisher(settings.redis_url, settings.redis_stream_key))
    else:
        log.warning("PRAHARI_MATCH_REDIS_URL not set; alerts fan out only to /api/v1/alerts")
    fan_out = FanOutPublisher(publishers) if len(publishers) > 1 else recent

    detection_publisher: DetectionPublisher
    if settings.redis_url:
        detection_publisher = RedisDetectionPublisher(
            settings.redis_url,
            settings.redis_detection_stream_key,
            settings.detection_stream_maxlen,
        )
    else:
        detection_publisher = NullDetectionPublisher()

    app.state.settings = settings
    app.state.store = store
    app.state.deduper = deduper
    app.state.recent_alerts = recent
    app.state.publisher = fan_out

    grpc_server = serve(store, deduper, fan_out, settings, detection_publisher=detection_publisher)
    app.state.grpc_server = grpc_server
    try:
        yield
    finally:
        grpc_server.stop(_GRPC_STOP_GRACE_S)


app = FastAPI(
    title="PRAHARI match engine",
    version="0.1.0",
    summary="Confusion-aware watchlist matching and alert fan-out",
    lifespan=lifespan,
)


# --- dependencies ------------------------------------------------------------


def get_store(request: Request) -> WatchlistStore:
    return request.app.state.store


def get_settings(request: Request) -> MatchSettings:
    return request.app.state.settings


def get_recent_alerts(request: Request) -> RecentAlertsPublisher:
    return request.app.state.recent_alerts


StoreDep = Annotated[WatchlistStore, Depends(get_store)]
SettingsDep = Annotated[MatchSettings, Depends(get_settings)]
RecentAlertsDep = Annotated[RecentAlertsPublisher, Depends(get_recent_alerts)]


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
async def readyz(store: StoreDep, response: Response) -> dict:
    """Readiness must report whether the watchlist actually loaded -- an empty
    watchlist means every detection is guaranteed to miss, which is a silent
    total failure indistinguishable from "nothing is on the watchlist today"
    unless this endpoint says so explicitly.
    """
    summary = _watchlist_summary(store)
    if summary["entries"] == 0:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "reason": "watchlist has 0 entries", **summary}
    return {"status": "ready", **summary}


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
    return {"status": "reloaded", **_watchlist_summary(store)}


# --- alerts ------------------------------------------------------------------


@app.get("/api/v1/alerts", tags=["alerts"])
async def list_alerts(recent: RecentAlertsDep, limit: int = Query(50, ge=1, le=500)) -> list[dict]:
    """Recent matches, newest first. Debug/admin surface -- the system of
    record is the Redis stream, when `PRAHARI_MATCH_REDIS_URL` is configured;
    this exists so a laptop run without Redis can still see what matched."""
    return [
        MessageToDict(alert, preserving_proto_field_name=True) for alert in recent.recent(limit)
    ]


@app.get("/api/v1/alerts/{alert_id}", tags=["alerts"])
async def get_alert(alert_id: str, recent: RecentAlertsDep) -> dict:
    for alert in recent.recent():
        if alert.alert_id == alert_id:
            return MessageToDict(alert, preserving_proto_field_name=True)
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"no alert {alert_id} in the recent buffer")
