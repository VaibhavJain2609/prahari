"""The correlation service's HTTP surface: liveness/readiness and route
reconstruction. REST only -- DAY3-DESIGN.md §3.6: correlation is not on the
gRPC high-rate path, its callers are BFF and (for gap data) itself calling
the registry.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.responses import PlainTextResponse
from prahari_common.internal_auth import expected_token_ok, provided_token

from .config import CorrelationSettings, correlation_settings
from .consumer import DetectionConsumer
from .db import PostgresSightings, apply_migrations, create_pool
from .metrics import Metrics
from .registry_client import RegistryClient
from .routes import RouteResult, build_route
from .store import DetectionStore, SightingSource

__all__ = ["app"]

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = correlation_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    metrics = Metrics()
    store = DetectionStore(
        max_per_plate=settings.max_detections_per_plate,
        max_plates=settings.max_tracked_plates,
        future_skew_allowance_s=settings.clock_skew_allowance_s,
        metrics=metrics,
    )

    # Durable sightings, when Postgres is configured. The schema is applied
    # in-process — same posture as the registry: either up with a correct
    # schema or not up at all. When unset the service runs memory-only and
    # /readyz says so.
    db: PostgresSightings | None = None
    if settings.database_url:
        pool = await create_pool(settings.database_url)
        applied = await apply_migrations(pool)
        if applied:
            log.info("applied migrations: %s", ", ".join(applied))
        db = PostgresSightings(pool)
    else:
        log.warning(
            "PRAHARI_CORRELATION_DATABASE_URL not set: sightings are memory-only "
            "and a restart loses all route history (stream position still "
            "survives via the consumer group, once Redis is reachable)"
        )

    consumer = DetectionConsumer(
        settings.redis_url,
        settings.redis_detection_stream_key,
        store,
        db=db,
        metrics=metrics,
    )
    consumer.start()

    # What route queries read: Postgres when configured (survives restarts,
    # unbounded by the LRU), the in-memory store otherwise.
    sightings: SightingSource = db if db is not None else store

    if not settings.internal_token:
        log.warning(
            "internal auth disabled: PRAHARI_CORRELATION_INTERNAL_TOKEN is unset, "
            "so /api/* is reachable by anything that can reach this pod"
        )

    registry = RegistryClient(
        settings.registry_base_url,
        settings.registry_timeout_s,
        settings.camera_location_cache_ttl_s,
        metrics=metrics,
        internal_token=settings.registry_internal_token,
    )

    metrics.gauge("store_plates", store.tracked_plate_count)
    metrics.gauge("store_unplated", store.unplated_count)
    metrics.gauge("detections_stream_length", consumer.stream_length)
    metrics.gauge("detections_pending", consumer.pending_count)

    app.state.settings = settings
    app.state.store = store
    app.state.sightings = sightings
    app.state.db = db
    app.state.consumer = consumer
    app.state.registry = registry
    app.state.metrics = metrics

    try:
        yield
    finally:
        await consumer.stop()
        await registry.aclose()
        if db is not None:
            await db.close()


app = FastAPI(
    title="PRAHARI correlation service",
    version="0.1.0",
    summary="Cross-camera route reconstruction",
    lifespan=lifespan,
)


@app.middleware("http")
async def require_internal_token(request: Request, call_next):
    """Same gate as the registry's `require_internal_token` — a route
    reconstruction is movement history, precisely what the BFF's authorisation
    exists to control, so this service must not be reachable directly once the
    token is armed.

    `/healthz` and `/readyz` are exempt — a probe carries no data and must not
    depend on a secret being wired correctly to answer. `/metrics` is exempt
    too: a Prometheus pod scrape cannot carry a credential (the observability
    NetworkPolicy restricts the port to the monitoring namespace on an
    enforcing CNI). Empty `internal_token` disables the gate entirely
    (`expected_token_ok`)."""
    # Tests that build the app without lifespan never set app.state.settings —
    # an absent settings object means an absent token, which is gate-off anyway.
    settings: CorrelationSettings | None = getattr(request.app.state, "settings", None)
    token = settings.internal_token if settings else ""
    if token and request.url.path not in ("/healthz", "/readyz", "/metrics"):
        if not expected_token_ok(provided_token(request.headers), token):
            return Response(
                status_code=status.HTTP_401_UNAUTHORIZED, content="internal token required"
            )
    return await call_next(request)


# --- dependencies ------------------------------------------------------------


def get_store(request: Request) -> SightingSource:
    """The sightings store route queries read — `PostgresSightings` when
    `database_url` is configured, the in-memory `DetectionStore` otherwise.
    The name is the pre-durability contract (`tests/test_day3_gate.py`
    overrides it to inject the query source); what it returns is whichever
    backend the lifespan wired into `app.state.sightings`."""
    return request.app.state.sightings


def get_db(request: Request) -> PostgresSightings | None:
    return getattr(request.app.state, "db", None)


def get_settings(request: Request) -> CorrelationSettings:
    return request.app.state.settings


def get_consumer(request: Request) -> DetectionConsumer:
    return request.app.state.consumer


def get_registry(request: Request) -> RegistryClient:
    return request.app.state.registry


def get_metrics(request: Request) -> Metrics:
    # A throwaway sink when the lifespan never ran (in-process ASGI transports
    # in the gate tests don't run it) — counters go nowhere, but a route query
    # must not 500 because a metrics handle was missing.
    return getattr(request.app.state, "metrics", None) or Metrics()


StoreDep = Annotated[SightingSource, Depends(get_store)]
DbDep = Annotated[PostgresSightings | None, Depends(get_db)]
SettingsDep = Annotated[CorrelationSettings, Depends(get_settings)]
ConsumerDep = Annotated[DetectionConsumer, Depends(get_consumer)]
RegistryDep = Annotated[RegistryClient, Depends(get_registry)]
MetricsDep = Annotated[Metrics, Depends(get_metrics)]


# --- probes ------------------------------------------------------------------


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict:
    """Liveness. Deliberately does not touch Redis or the registry -- same
    reasoning as every other service's `/healthz` here: a liveness probe
    must never depend on something a mere reconnect, not a restart, would
    fix."""
    return {"status": "ok", "service": "correlation"}


@app.get("/readyz", tags=["ops"])
async def readyz(consumer: ConsumerDep, db: DbDep, response: Response) -> dict:
    """Ready means: consuming, and persistence is what it claims to be.

    The consumer check is unchanged — a service that cannot consume returns
    empty routes forever. On top of it, `persistence` reports which sightings
    backend queries read: `postgres` (and the DB is actually reachable — a
    configured-but-down database means the consumer can only build pending
    backlog, which is not ready) or `in-memory` (no `database_url`: still
    serving, but a restart loses all route history — said out loud rather
    than implied)."""
    if not consumer.is_connected():
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "reason": "detection consumer not connected to redis"}
    if db is not None:
        try:
            if not await db.ping():
                raise ConnectionError("postgres ping returned falsy")
        except Exception:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {
                "status": "unavailable",
                "reason": "postgres unreachable",
                "persistence": "postgres",
            }
        return {"status": "ready", "persistence": "postgres"}
    return {"status": "ready", "persistence": "in-memory"}


# --- metrics -----------------------------------------------------------------


@app.get("/metrics", tags=["ops"], response_class=PlainTextResponse)
async def metrics_endpoint(metrics: MetricsDep) -> PlainTextResponse:
    """Plaintext counters/gauges (`prahari_correlation_*` lines), hand-rolled
    rather than via a prometheus client. Deliberately a route on this app,
    not a second port: one HTTP surface to expose and to lock down."""
    return PlainTextResponse(metrics.render())


# --- routes --------------------------------------------------------------------


def _route_to_dict(result: RouteResult) -> dict:
    return {
        "plate": result.plate,
        "ungated_hops": result.ungated_hops,
        "hops": [
            {
                "camera_id": hop.camera_id,
                "location": (
                    {"latitude": hop.location.latitude, "longitude": hop.location.longitude}
                    if hop.location
                    else None
                ),
                "wall_clock_s": hop.wall_clock_s,
                "pts_ms": hop.pts_ms,
                "link_kind": hop.link_kind.value if hop.link_kind else None,
                "confidence": hop.confidence,
                "evidence_ref": hop.evidence_ref,
                "first_seen_s": hop.first_seen_s,
                "last_seen_s": hop.last_seen_s,
                "sightings": hop.sightings,
            }
            for hop in result.hops
        ],
        "rejected": [
            {
                "from_camera_id": r.from_camera_id,
                "to_camera_id": r.to_camera_id,
                "reason": r.reason,
                "implied_speed_kmh": r.implied_speed_kmh,
            }
            for r in result.rejected
        ],
        "dark_zones": [
            {
                "camera_id": z.camera_id,
                "location": (
                    {"latitude": z.location.latitude, "longitude": z.location.longitude}
                    if z.location
                    else None
                ),
            }
            for z in result.dark_zones
        ],
    }


@app.get("/api/v1/routes/{plate}", tags=["routes"])
async def get_route(
    plate: str,
    sightings: StoreDep,
    registry: RegistryDep,
    settings: SettingsDep,
    metrics: MetricsDep,
    since_s: float | None = None,
) -> dict:
    """`since_s` (epoch seconds) bounds the history window explicitly — a
    caller that wants "yesterday" says so. Absent, the whole persisted
    history is searched, capped at `route_history_max_sightings` of the most
    recent sightings."""
    metrics.inc("route_queries")
    result = await build_route(
        plate,
        sightings,
        registry,
        settings.max_speed_kmh,
        settings.appearance_similarity_threshold,
        clock_skew_allowance_s=settings.clock_skew_allowance_s,
        since_s=since_s,
        max_sightings=settings.route_history_max_sightings,
    )
    metrics.inc("rejected_hops", len(result.rejected))
    metrics.inc("ungated_hops", result.ungated_hops)
    return _route_to_dict(result)
