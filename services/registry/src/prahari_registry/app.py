"""The registry HTTP API.

REST/JSON because the browser talks to this through the BFF. The high-rate
worker→match-engine link is gRPC + protobuf; camera registration and health are
low-rate and human-facing, and JSON keeps the console and `curl` on equal terms.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Annotated

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from prahari_common.config import GatewaySettings
from pydantic import ValidationError

from . import gaps
from .config import RegistrySettings, registry_settings
from .db import apply_migrations, create_pool, timescale_available
from .health import HealthPolicy, derive_state
from .mediamtx import MediaMTXClient
from .models import (
    Camera,
    CameraCreate,
    CameraUpdate,
    DarkZone,
    DistrictCoverage,
    HealthState,
    Heartbeat,
    HeartbeatAck,
    Lifecycle,
    NearestCamera,
    SyncResult,
)
from .repository import CameraRepository
from .sync import CatalogueSync

log = logging.getLogger(__name__)


def _gateway_settings_or_none() -> GatewaySettings | None:
    """Gateway credentials are optional at startup.

    The registry has real work to do without them — manual (Model 2) camera
    registration, health tracking, gap analysis — and refusing to start would
    mean a missing secret takes down the map as well as the sync. The absence is
    logged loudly and surfaced on /healthz instead.
    """
    try:
        return GatewaySettings()  # type: ignore[call-arg]
    except ValidationError:
        return None


async def _retention_loop(repo: CameraRepository, settings: RegistrySettings) -> None:
    """Keep `camera_heartbeat` bounded where TimescaleDB is not doing it for us.

    Runs in every replica. That is harmless — the DELETE is idempotent and the
    losers of the race simply delete nothing — and it avoids making retention
    depend on which pod happens to be the leader.
    """
    while True:
        await asyncio.sleep(settings.heartbeat_prune_interval_s)
        try:
            deleted = await repo.prune_heartbeats(retention_days=settings.heartbeat_retention_days)
            if deleted:
                log.info(
                    "pruned %d heartbeats older than %d days",
                    deleted,
                    settings.heartbeat_retention_days,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("heartbeat prune failed; retrying next pass")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: RegistrySettings = registry_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    pool = await create_pool(settings)
    applied = await apply_migrations(pool)
    if applied:
        log.info("applied migrations: %s", ", ".join(applied))
    if not await timescale_available(pool):
        log.warning(
            "timescaledb not present: camera_heartbeat is a plain table. "
            "Retention falls back to the in-process pruner (%d days); queries over "
            "long windows will be slower than on a hypertable.",
            settings.heartbeat_retention_days,
        )

    repo = CameraRepository(pool, settings)
    gateway = _gateway_settings_or_none()
    mediamtx = MediaMTXClient(settings)
    sync = CatalogueSync(
        pool=pool, repo=repo, settings=settings, gateway=gateway, mediamtx=mediamtx
    )

    app.state.pool = pool
    app.state.settings = settings
    app.state.repo = repo
    app.state.sync = sync
    app.state.mediamtx = mediamtx
    app.state.gateway_configured = gateway is not None

    sync.start()
    retention = asyncio.create_task(_retention_loop(repo, settings), name="heartbeat-retention")
    try:
        yield
    finally:
        retention.cancel()
        await sync.stop()
        await pool.close()


app = FastAPI(
    title="PRAHARI camera registry",
    version="0.1.0",
    summary="Camera catalogue, GIS coverage and live health for the Gujarat estate",
    lifespan=lifespan,
)


# --- dependencies ------------------------------------------------------------


def get_repo(request: Request) -> CameraRepository:
    return request.app.state.repo


def get_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


def get_settings(request: Request) -> RegistrySettings:
    return request.app.state.settings


def get_sync(request: Request) -> CatalogueSync:
    return request.app.state.sync


RepoDep = Annotated[CameraRepository, Depends(get_repo)]
PoolDep = Annotated[asyncpg.Pool, Depends(get_pool)]
SettingsDep = Annotated[RegistrySettings, Depends(get_settings)]
SyncDep = Annotated[CatalogueSync, Depends(get_sync)]


def _parse_bbox(bbox: str | None) -> tuple[float, float, float, float] | None:
    """`min_lon,min_lat,max_lon,max_lat` — MapLibre's `getBounds().toArray()`
    order, flattened. Longitude first, which is the ordering that silently puts
    a whole district in the wrong hemisphere when it is got wrong."""
    if not bbox:
        return None
    parts = bbox.split(",")
    if len(parts) != 4:
        raise HTTPException(422, "bbox must be 'min_lon,min_lat,max_lon,max_lat'")
    try:
        min_lon, min_lat, max_lon, max_lat = (float(p) for p in parts)
    except ValueError as exc:
        raise HTTPException(422, f"bbox values must be numbers: {exc}") from exc
    return min_lon, min_lat, max_lon, max_lat


# --- probes ------------------------------------------------------------------


@app.get("/healthz", tags=["ops"])
async def healthz(request: Request) -> dict:
    """Liveness. Deliberately does NOT touch the database.

    A liveness probe that fails when Postgres is unreachable restarts every
    registry pod during a database blip, turning a recoverable outage into a
    crash loop. Database reachability is a *readiness* question, below.
    """
    return {
        "status": "ok",
        "service": "registry",
        "gateway_configured": request.app.state.gateway_configured,
    }


@app.get("/readyz", tags=["ops"])
async def readyz(pool: PoolDep, response: Response) -> dict:
    try:
        await pool.fetchval("SELECT 1")
    except (asyncpg.PostgresError, OSError) as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "database": f"{type(exc).__name__}: {exc}"}
    return {"status": "ready", "database": "ok"}


# --- cameras -----------------------------------------------------------------


@app.get("/api/v1/cameras", response_model=list[Camera], tags=["cameras"])
async def list_cameras(
    repo: RepoDep,
    district: str | None = None,
    department: str | None = None,
    state: HealthState | None = None,
    lifecycle: Lifecycle | None = Lifecycle.ACTIVE,
    bbox: str | None = Query(None, description="min_lon,min_lat,max_lon,max_lat"),
    search: str | None = None,
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> list[Camera]:
    return await repo.list(
        district=district,
        department=department,
        state=state,
        lifecycle=lifecycle,
        bbox=_parse_bbox(bbox),
        search=search,
        limit=limit,
        offset=offset,
    )


@app.get("/api/v1/cameras/summary", tags=["cameras"])
async def camera_summary(repo: RepoDep) -> dict:
    """Headline counts for the console's status bar."""
    return {
        "active": await repo.count(lifecycle=Lifecycle.ACTIVE),
        "absent": await repo.count(lifecycle=Lifecycle.ABSENT),
        "decommissioned": await repo.count(lifecycle=Lifecycle.DECOMMISSIONED),
        "health": await repo.health_summary(),
    }


# Declared before /{camera_id} so the literal path is not swallowed by the
# parameterised one.
@app.get("/api/v1/cameras/geojson", tags=["cameras"])
async def cameras_geojson(
    pool: PoolDep,
    bbox: str | None = Query(None, description="min_lon,min_lat,max_lon,max_lat"),
    limit: int = Query(20_000, ge=1, le=100_000),
) -> dict:
    return await gaps.cameras_geojson(pool, bbox=_parse_bbox(bbox), limit=limit)


@app.post(
    "/api/v1/cameras",
    response_model=Camera,
    status_code=status.HTTP_201_CREATED,
    tags=["cameras"],
)
async def create_camera(payload: CameraCreate, repo: RepoDep) -> Camera:
    """Register a camera by hand.

    Reference Model 2 (direct connect) and the large analog estate behind DVRs
    arrive this way. Once registered they are indistinguishable to everything
    downstream, which is what "vendor-neutral registry" has to mean to be worth
    claiming.
    """
    existing = await repo.get_by_external(payload.source, payload.external_id)
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"camera {payload.source}/{payload.external_id} already registered as {existing.id}",
        )
    return await repo.create(payload)


@app.get("/api/v1/cameras/{camera_id}", response_model=Camera, tags=["cameras"])
async def get_camera(camera_id: str, repo: RepoDep) -> Camera:
    camera = await repo.get(camera_id)
    if camera is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no camera {camera_id}")
    return camera


@app.patch("/api/v1/cameras/{camera_id}", response_model=Camera, tags=["cameras"])
async def update_camera(camera_id: str, payload: CameraUpdate, repo: RepoDep) -> Camera:
    camera = await repo.update(camera_id, payload)
    if camera is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no camera {camera_id}")
    return camera


@app.delete("/api/v1/cameras/{camera_id}", response_model=Camera, tags=["cameras"])
async def decommission_camera(camera_id: str, repo: RepoDep) -> Camera:
    """Retire a camera. This is a soft delete and always will be: its detections
    are evidence, and evidence pointing at a deleted camera cannot be defended."""
    camera = await repo.decommission(camera_id)
    if camera is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no camera {camera_id}")
    return camera


# --- health ------------------------------------------------------------------


@app.post("/api/v1/cameras/{camera_id}/heartbeat", response_model=HeartbeatAck, tags=["health"])
async def post_heartbeat(
    camera_id: str, heartbeat: Heartbeat, repo: RepoDep, settings: SettingsDep
) -> HeartbeatAck:
    """Accept one health report from an ingest worker.

    Workers report observations; the registry decides state. That split matters
    for two reasons: two workers on the same camera must not be able to publish
    contradictory verdicts, and a worker cannot observe that its own heartbeats
    have stopped arriving — which is exactly the failure that matters most.

    At estate scale this moves onto the bus (80,000 cameras at one report per
    10 s is ~8k writes/s). At demo scale HTTP is honest and debuggable, and the
    protobuf message is already defined for the day it moves.
    """
    camera = await repo.get(camera_id)
    if camera is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no camera {camera_id}")

    policy = HealthPolicy(
        fps_drift_ratio=settings.health_fps_drift_ratio,
        fps_baseline_min_samples=settings.health_fps_baseline_min_samples,
        black_frame_ratio=settings.health_black_frame_ratio,
        tamper_confirm_heartbeats=settings.health_tamper_confirm_heartbeats,
    )
    recent_fps, recent_tamper = await repo.recent_health_history(
        camera_id,
        window_s=settings.health_fps_baseline_window_s,
        limit=max(60, policy.tamper_confirm_heartbeats),
    )
    verdict = derive_state(
        heartbeat,
        recent_fps=recent_fps,
        recent_tamper_flags=recent_tamper,
        policy=policy,
    )
    await repo.record_heartbeat(camera_id, heartbeat, verdict)
    return HeartbeatAck(
        camera_id=camera_id,
        state=verdict.state,
        reason=verdict.reason,
        baseline_fps=verdict.baseline_fps,
    )


# --- catalogue sync ----------------------------------------------------------


@app.post("/api/v1/sync", response_model=SyncResult, tags=["sync"])
async def trigger_sync(sync: SyncDep, request: Request) -> SyncResult:
    """Sync now. Idempotent, so pressing it twice during a demo is safe."""
    if not request.app.state.gateway_configured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "gateway credentials not configured; set PRAHARI_GATEWAY_HOST and "
            "PRAHARI_GATEWAY_PASSWORD (see .env.example)",
        )
    result = await sync.run_once_locked()
    if result is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "a sync is already running")
    return result


@app.get("/api/v1/sync/runs", response_model=list[SyncResult], tags=["sync"])
async def sync_runs(repo: RepoDep, limit: int = Query(10, ge=1, le=100)) -> list[SyncResult]:
    """Recent syncs, newest first. Shows ids rotating between runs — the
    concrete evidence for why nothing constructs a stream URL from a template."""
    return await repo.last_sync_runs(limit)


# --- fan-out -----------------------------------------------------------------


@app.get("/api/v1/streams/paths", tags=["streams"])
async def stream_paths(repo: RepoDep) -> dict[str, str]:
    """The MediaMTX paths this registry wants to exist. Diagnostic: comparing
    this against MediaMTX's own list tells you whether a missing preview is a
    registry problem or a restreamer problem."""
    return await repo.desired_mediamtx_paths()


@app.post("/api/v1/streams/reconcile", tags=["streams"])
async def reconcile_streams(repo: RepoDep, request: Request) -> dict:
    mediamtx: MediaMTXClient = request.app.state.mediamtx
    result = await mediamtx.reconcile(await repo.desired_mediamtx_paths())
    return result.__dict__


# --- gap analysis ------------------------------------------------------------


@app.get("/api/v1/gaps/districts", response_model=list[DistrictCoverage], tags=["gaps"])
async def district_coverage(pool: PoolDep) -> list[DistrictCoverage]:
    return await gaps.district_coverage(pool)


@app.get("/api/v1/gaps/dark-zones", response_model=list[DarkZone], tags=["gaps"])
async def dark_zones(
    pool: PoolDep, settings: SettingsDep, radius_m: float | None = Query(None, gt=0)
) -> list[DarkZone]:
    """Cameras that are down with no healthy camera nearby.

    The radius is a parameter because the right answer differs by context: a few
    hundred metres in a city centre, several kilometres on a highway corridor
    where cameras are sparse by design and a gap is not a fault.
    """
    return await gaps.dark_zones(pool, radius_m=radius_m or settings.gap_dark_zone_radius_m)


@app.get("/api/v1/gaps/nearest", response_model=list[NearestCamera], tags=["gaps"])
async def nearest(
    pool: PoolDep,
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    limit: int = Query(5, ge=1, le=50),
    healthy_only: bool = True,
) -> list[NearestCamera]:
    return await gaps.nearest_cameras(
        pool, latitude=lat, longitude=lon, limit=limit, healthy_only=healthy_only
    )
