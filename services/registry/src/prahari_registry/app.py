"""The registry HTTP API.

REST/JSON because the browser talks to this through the BFF. The high-rate
worker→match-engine link is gRPC + protobuf; camera registration and health are
low-rate and human-facing, and JSON keeps the console and `curl` on equal terms.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse
from prahari_common.config import GatewaySettings
from pydantic import ValidationError

from . import gaps
from .config import RegistrySettings, registry_settings
from .crypto import CredentialKeyError
from .db import apply_migrations, create_pool, timescale_available
from .health import HealthPolicy, derive_state
from .media_auth import MediaMTXAuthRequest, TicketVerifier, authorize
from .mediamtx import MediaMTXClient
from .metrics import HEARTBEATS_RECEIVED, METRICS, refresh_gauges
from .models import (
    Camera,
    CameraCreate,
    CameraProbeRequest,
    CameraUpdate,
    DarkZone,
    DistrictCoverage,
    HealthState,
    Heartbeat,
    HeartbeatAck,
    HeartbeatSample,
    Lifecycle,
    NearestCamera,
    Org,
    OrgCreate,
    SyncResult,
    WorkerAssignment,
    WorkerRegister,
    WorkerRegistration,
)
from .probe import ProbeError, ProbeResult, SSRFBlockedError, probe_rtsp
from .repository import (
    CameraRepository,
    OrgRepository,
    WorkerRepository,
    redact_url_credentials,
)
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


async def _retention_loop(
    repo: CameraRepository, worker_repo: WorkerRepository, settings: RegistrySettings
) -> None:
    """Keep `camera_heartbeat` and `workers` bounded where nothing else does.

    Runs in every replica. That is harmless — the DELETEs are idempotent and
    the losers of the race simply delete nothing — and it avoids making
    retention depend on which pod happens to be the leader.
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
            reaped = await worker_repo.prune_stale()
            if reaped:
                log.info(
                    "reaped %d worker registration(s) idle for >%dx the assignment lease",
                    reaped,
                    WorkerRepository._REAP_LEASES,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("heartbeat prune failed; retrying next pass")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: RegistrySettings = registry_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not settings.internal_token:
        log.warning(
            "internal API unauthenticated (PRAHARI_INTERNAL_TOKEN unset) — "
            "every /api/* request is accepted without an X-Internal-Token "
            "header. Acceptable only where nothing off-cluster can reach this "
            "service; every real profile must set a shared token."
        )

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
    org_repo = OrgRepository(pool)
    worker_repo = WorkerRepository(pool, settings)
    gateway = _gateway_settings_or_none()
    mediamtx = MediaMTXClient(settings)
    ticket_verifier = TicketVerifier(settings)
    sync = CatalogueSync(
        pool=pool, repo=repo, settings=settings, gateway=gateway, mediamtx=mediamtx
    )

    app.state.pool = pool
    app.state.settings = settings
    app.state.repo = repo
    app.state.org_repo = org_repo
    app.state.worker_repo = worker_repo
    app.state.sync = sync
    app.state.mediamtx = mediamtx
    app.state.ticket_verifier = ticket_verifier
    app.state.gateway_configured = gateway is not None

    sync.start()
    retention = asyncio.create_task(
        _retention_loop(repo, worker_repo, settings), name="heartbeat-retention"
    )
    try:
        yield
    finally:
        retention.cancel()
        await sync.stop()
        await ticket_verifier.aclose()
        await pool.close()


app = FastAPI(
    title="PRAHARI camera registry",
    version="0.1.0",
    summary="Camera catalogue, GIS coverage and live health for the Gujarat estate",
    lifespan=lifespan,
)


@app.middleware("http")
async def require_internal_token(request: Request, call_next):
    """Makes the registry cluster-internal-only once `internal_token` is set
    — see `RegistrySettings.internal_token` and docs/ORG-TIERS-DESIGN.md §3.3.
    Without this, the BFF's org-scope check is decorative: a browser (or
    anything else) could simply call the registry directly and read the
    whole estate unscoped.

    Semantics are deliberately asymmetric:

    * **Token configured → fail closed.** Any request other than `/healthz`
      `/readyz` without a byte-identical `X-Internal-Token` gets a 401. The
      comparison is `hmac.compare_digest` — a `!=` string compare leaks the
      token a byte at a time through response timing.
    * **Token empty → enforcement is OFF.** Requests pass with no check.
      This is the local/dev default and is logged loudly at startup; it is
      NOT fail-closed, which is why every real profile must set a token.

    `/healthz` and `/readyz` are exempt — a liveness/readiness probe carries
    no data and must not depend on a secret being wired correctly to answer.
    `/metrics` is exempt for the same reason: a Prometheus scrape cannot hold
    the credential.
    `/api/v1/mediamtx/auth` is exempt too: it IS the credential check
    MediaMTX defers to (`authMethod: http`), and the restreamer cannot send
    this header — gating it would deadlock the video plane. Everything else
    under `/api/*` (and, deliberately, everything not yet under `/api/*`)
    requires the header when a token is configured.
    """
    settings: RegistrySettings = request.app.state.settings
    if settings.internal_token and request.url.path not in (
        "/healthz",
        "/readyz",
        "/metrics",
        "/api/v1/mediamtx/auth",
    ):
        provided = request.headers.get("x-internal-token")
        if provided is None or not hmac.compare_digest(
            provided.encode(), settings.internal_token.encode()
        ):
            return Response(
                status_code=status.HTTP_401_UNAUTHORIZED, content="internal token required"
            )
    return await call_next(request)


@app.exception_handler(asyncpg.DataError)
async def _data_error_handler(request: Request, exc: asyncpg.DataError) -> JSONResponse:
    """A malformed identifier reaching a `$1::uuid` / `$2::ltree` cast surfaces
    from asyncpg as `DataError` (`InvalidTextRepresentationError`, PG 22P02)
    rather than as a routed 4xx — without this handler it is a 500.

    * Bad `ltree` (`?org_scope=...`) → 422: the *query parameter* is malformed;
      a 404 would tell the client the camera does not exist when in fact the
      scope itself was never valid.
    * Anything else (a non-UUID `camera_id`/`org_id` in the path) → 404: no
      object can ever have that id, which is what "not found" means.
    """
    message = str(exc)
    if "ltree" in message:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": "org_scope is not a valid ltree path"},
        )
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": "not found or malformed identifier"},
    )


# --- dependencies ------------------------------------------------------------


def get_repo(request: Request) -> CameraRepository:
    return request.app.state.repo


def get_org_repo(request: Request) -> OrgRepository:
    return request.app.state.org_repo


def get_worker_repo(request: Request) -> WorkerRepository:
    return request.app.state.worker_repo


def get_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


def get_settings(request: Request) -> RegistrySettings:
    return request.app.state.settings


def get_sync(request: Request) -> CatalogueSync:
    return request.app.state.sync


RepoDep = Annotated[CameraRepository, Depends(get_repo)]
OrgRepoDep = Annotated[OrgRepository, Depends(get_org_repo)]
WorkerRepoDep = Annotated[WorkerRepository, Depends(get_worker_repo)]
PoolDep = Annotated[asyncpg.Pool, Depends(get_pool)]
SettingsDep = Annotated[RegistrySettings, Depends(get_settings)]
SyncDep = Annotated[CatalogueSync, Depends(get_sync)]


def get_scope(
    request: Request,
    org_scope: str | None = Query(
        None,
        description="ltree org path to scope this read/write to. "
        "Provisional: Stage 2 replaces this with the caller's own org, "
        "resolved from their session or API key, so a client cannot simply "
        "widen its own scope by editing a query parameter.",
    ),
) -> str:
    """Where scope comes from until there is a `Principal`.

    Every repository/gaps call now *requires* a scope argument — that
    invariant is real starting today. What is provisional is only where the
    value comes from: an unauthenticated query parameter, defaulting to the
    state root, rather than a verified identity. Stage 2 (BFF) swaps this
    dependency for one that reads a session or API key and ignores anything
    the client claims about its own scope. Nothing downstream changes when
    that happens — `list`, `get`, `district_coverage` etc. take the same
    `scope: str` either way.
    """
    if org_scope:
        return org_scope
    settings: RegistrySettings = request.app.state.settings
    return settings.sync_default_org_path


ScopeDep = Annotated[str, Depends(get_scope)]


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
        # Exception text can carry DB internals (hostnames, query fragments) —
        # this endpoint answers unauthenticated callers, so log it, don't leak it.
        log.warning("readyz: database check failed: %s", exc)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "database": "error"}
    return {"status": "ready", "database": "ok"}


@app.get("/metrics", tags=["ops"], response_class=PlainTextResponse)
async def metrics(request: Request) -> str:
    """Prometheus text exposition — heartbeats accepted, live ingest workers,
    cameras by effective health state, desired MediaMTX paths. Exempt from
    the token gate for the same reason `/healthz` is: a scrape cannot hold a
    credential. DB-backed gauges are refreshed here at scrape time; a failed
    refresh serves the last-known values rather than answering 500."""
    await refresh_gauges(request.app.state)
    return METRICS.render()


# --- orgs ----------------------------------------------------------------


@app.post("/api/v1/orgs", response_model=Org, status_code=status.HTTP_201_CREATED, tags=["orgs"])
async def create_org(payload: OrgCreate, org_repo: OrgRepoDep) -> Org:
    try:
        return await org_repo.create(payload)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@app.get("/api/v1/orgs", response_model=list[Org], tags=["orgs"])
async def list_orgs(org_repo: OrgRepoDep, scope: ScopeDep) -> list[Org]:
    """Every org at or below `scope` — the org-admin screen's tree."""
    return await org_repo.list_subtree(scope)


@app.get("/api/v1/orgs/{org_id}", response_model=Org, tags=["orgs"])
async def get_org(org_id: str, org_repo: OrgRepoDep) -> Org:
    org = await org_repo.get(org_id)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no org {org_id}")
    return org


# --- cameras -----------------------------------------------------------------


@app.get("/api/v1/cameras", response_model=list[Camera], tags=["cameras"])
async def list_cameras(
    repo: RepoDep,
    scope: ScopeDep,
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
        scope=scope,
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
async def camera_summary(repo: RepoDep, scope: ScopeDep) -> dict:
    """Headline counts for the console's status bar."""
    return {
        "active": await repo.count(scope=scope, lifecycle=Lifecycle.ACTIVE),
        "absent": await repo.count(scope=scope, lifecycle=Lifecycle.ABSENT),
        "decommissioned": await repo.count(scope=scope, lifecycle=Lifecycle.DECOMMISSIONED),
        "health": await repo.health_summary(scope=scope),
    }


# Declared before /{camera_id} so the literal path is not swallowed by the
# parameterised one.
@app.get("/api/v1/cameras/geojson", tags=["cameras"])
async def cameras_geojson(
    pool: PoolDep,
    scope: ScopeDep,
    bbox: str | None = Query(None, description="min_lon,min_lat,max_lon,max_lat"),
    limit: int = Query(20_000, ge=1, le=100_000),
) -> dict:
    return await gaps.cameras_geojson(pool, scope=scope, bbox=_parse_bbox(bbox), limit=limit)


@app.post(
    "/api/v1/cameras",
    response_model=Camera,
    status_code=status.HTTP_201_CREATED,
    tags=["cameras"],
)
async def create_camera(
    payload: CameraCreate, repo: RepoDep, org_repo: OrgRepoDep, settings: SettingsDep
) -> Camera:
    """Register a camera by hand.

    Reference Model 2 (direct connect) and the large analog estate behind DVRs
    arrive this way. Once registered they are indistinguishable to everything
    downstream, which is what "vendor-neutral registry" has to mean to be worth
    claiming.
    """
    if payload.org_id is not None:
        target_org = await org_repo.get(payload.org_id)
        if target_org is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no org {payload.org_id}")
        target_scope = target_org.path
    else:
        target_scope = settings.sync_default_org_path

    existing = await repo.get_by_external(payload.source, payload.external_id, scope=target_scope)
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"camera {payload.source}/{payload.external_id} already registered as {existing.id}",
        )
    try:
        return await repo.create(payload)
    except CredentialKeyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@app.post("/api/v1/cameras/probe", response_model=ProbeResult, tags=["cameras"])
async def probe_camera(payload: CameraProbeRequest, settings: SettingsDep) -> ProbeResult:
    """SSRF-hardened connectivity check — see `probe.py` for the guard. No
    scope/role check here: this touches no camera or org data, it is a
    bare network probe of a caller-supplied URL. The BFF proxy is where
    operator role + purpose code are actually enforced, since this service
    has no principal concept at all (by design — see the module docstring
    on any of the camera write handlers below)."""
    try:
        return await probe_rtsp(
            payload.rtsp_url,
            username=payload.username,
            password=payload.password,
            allowed_ports=settings.probe_allowed_ports,
        )
    except SSRFBlockedError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except ProbeError as exc:
        return ProbeResult(reachable=False, status_message=str(exc))


@app.get("/api/v1/cameras/{camera_id}", response_model=Camera, tags=["cameras"])
async def get_camera(camera_id: str, repo: RepoDep, scope: ScopeDep) -> Camera:
    camera = await repo.get(camera_id, scope=scope)
    if camera is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no camera {camera_id}")
    return camera


@app.patch("/api/v1/cameras/{camera_id}", response_model=Camera, tags=["cameras"])
async def update_camera(
    camera_id: str, payload: CameraUpdate, repo: RepoDep, scope: ScopeDep
) -> Camera:
    try:
        camera = await repo.update(camera_id, payload, scope=scope)
    except CredentialKeyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if camera is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no camera {camera_id}")
    return camera


@app.delete("/api/v1/cameras/{camera_id}", response_model=Camera, tags=["cameras"])
async def decommission_camera(camera_id: str, repo: RepoDep, scope: ScopeDep) -> Camera:
    """Retire a camera. This is a soft delete and always will be: its detections
    are evidence, and evidence pointing at a deleted camera cannot be defended."""
    camera = await repo.decommission(camera_id, scope=scope)
    if camera is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no camera {camera_id}")
    return camera


# --- health ------------------------------------------------------------------


@app.post("/api/v1/cameras/{camera_id}/heartbeat", response_model=HeartbeatAck, tags=["health"])
async def post_heartbeat(
    camera_id: str, heartbeat: Heartbeat, repo: RepoDep, settings: SettingsDep, scope: ScopeDep
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
    camera = await repo.get(camera_id, scope=scope)
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
    METRICS.inc(HEARTBEATS_RECEIVED)
    return HeartbeatAck(
        camera_id=camera_id,
        state=verdict.state,
        reason=verdict.reason,
        baseline_fps=verdict.baseline_fps,
    )


@app.get(
    "/api/v1/cameras/{camera_id}/health-history",
    response_model=list[HeartbeatSample],
    tags=["health"],
)
async def health_history(
    camera_id: str,
    repo: RepoDep,
    scope: ScopeDep,
    limit: int = Query(100, ge=1, le=500),
    since: Annotated[
        datetime | None,
        Query(description="Only heartbeats at or after this timestamp (ISO-8601)"),
    ] = None,
) -> list[HeartbeatSample]:
    """Recent heartbeats for one camera, newest first — the console's camera
    detail drawer. Scoped exactly like `get_camera`: the camera must be
    visible under `scope` before any of its history is."""
    camera = await repo.get(camera_id, scope=scope)
    if camera is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no camera {camera_id}")
    return await repo.health_history(camera_id, scope=scope, since=since, limit=limit)


# --- worker assignment ---------------------------------------------------------
#
# Worker-facing, and behind the same X-Internal-Token gate as everything else
# under /api/* — no separate middleware branch. Two endpoints instead of one
# combined call because register is cheap (one upsert) and idempotent: a worker
# refreshes its lease every assignment cycle, and an operator can ask "what
# shard does pod X think it owns" without pulling the camera list.


@app.post("/api/v1/workers/register", response_model=WorkerRegistration, tags=["workers"])
async def register_worker(
    payload: WorkerRegister, worker_repo: WorkerRepoDep
) -> WorkerRegistration:
    """Join or refresh the ingest pool; returns the worker's shard coordinates.

    Idempotent by construction — the keep-alive IS re-registering, so a worker
    calls this at boot and on every assignment-refresh tick. `shard_index` and
    `shard_count` are recomputed from the alive set on every call: a pod that
    missed its lease simply drops out of `shard_count` on the next call, which
    is the whole reaper — no DELETE endpoint exists because a killed pod cannot
    be relied on to make one last request.
    """
    return await worker_repo.register(payload.worker_id)


@app.get("/api/v1/assignments", response_model=WorkerAssignment, tags=["workers"])
async def worker_assignments(
    worker_id: Annotated[
        str,
        Query(min_length=1, description="the id this worker registered with"),
    ],
    worker_repo: WorkerRepoDep,
    settings: SettingsDep,
) -> WorkerAssignment:
    """This worker's slice of the active camera estate.

    Register-or-refresh happens inside the call, so polling this endpoint alone
    keeps the lease warm. The scope is the estate root — workers pull for the
    whole registry, not one org subtree — taken from settings rather than the
    provisional `org_scope` parameter, which a worker has no business widening.
    """
    return await worker_repo.assignment(worker_id, scope=settings.sync_default_org_path)


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
    registry problem or a restreamer problem.

    `desired_mediamtx_paths` embeds *decrypted* DVR credentials in each URL —
    MediaMTX needs them to pull the source. This endpoint strips userinfo from
    every value before serialising; the reconcile path keeps the credentialed
    form."""
    paths = await repo.desired_mediamtx_paths()
    return {name: redact_url_credentials(url) for name, url in paths.items()}


@app.post("/api/v1/streams/reconcile", tags=["streams"])
async def reconcile_streams(repo: RepoDep, request: Request) -> dict:
    mediamtx: MediaMTXClient = request.app.state.mediamtx
    result = await mediamtx.reconcile(await repo.desired_mediamtx_paths())
    return result.__dict__


@app.post("/api/v1/mediamtx/auth", tags=["streams"])
async def mediamtx_auth(
    payload: MediaMTXAuthRequest, settings: SettingsDep, request: Request
) -> Response:
    """The credential check MediaMTX defers to (`authMethod: http`).

    Answers 200 to allow, 401 to refuse — MediaMTX treats any non-2xx as a
    refusal. Exempt from `require_internal_token` by necessity (see the
    middleware): the restreamer cannot hold the token it is asking us to
    check. The policy itself lives in `media_auth.authorize`.
    """
    verifier: TicketVerifier = request.app.state.ticket_verifier
    allowed = await authorize(settings, verifier, payload)
    if not allowed:
        log.info(
            "mediamtx auth denied: action=%s path=%s user=%s ip=%s",
            payload.action,
            payload.path,
            payload.user,
            payload.ip,
        )
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)
    return Response(status_code=status.HTTP_200_OK)


# --- gap analysis ------------------------------------------------------------


@app.get("/api/v1/gaps/districts", response_model=list[DistrictCoverage], tags=["gaps"])
async def district_coverage(pool: PoolDep, scope: ScopeDep) -> list[DistrictCoverage]:
    return await gaps.district_coverage(pool, scope=scope)


@app.get("/api/v1/gaps/dark-zones", response_model=list[DarkZone], tags=["gaps"])
async def dark_zones(
    pool: PoolDep,
    settings: SettingsDep,
    scope: ScopeDep,
    radius_m: float | None = Query(None, gt=0),
) -> list[DarkZone]:
    """Cameras that are down with no healthy camera nearby.

    The radius is a parameter because the right answer differs by context: a few
    hundred metres in a city centre, several kilometres on a highway corridor
    where cameras are sparse by design and a gap is not a fault.
    """
    radius = radius_m or settings.gap_dark_zone_radius_m
    return await gaps.dark_zones(pool, scope=scope, radius_m=radius)


@app.get("/api/v1/gaps/nearest", response_model=list[NearestCamera], tags=["gaps"])
async def nearest(
    pool: PoolDep,
    scope: ScopeDep,
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    limit: int = Query(5, ge=1, le=50),
    healthy_only: bool = True,
) -> list[NearestCamera]:
    return await gaps.nearest_cameras(
        pool, scope=scope, latitude=lat, longitude=lon, limit=limit, healthy_only=healthy_only
    )
