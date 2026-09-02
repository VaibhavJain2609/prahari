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

from .config import CorrelationSettings, correlation_settings
from .consumer import DetectionConsumer
from .registry_client import RegistryClient
from .routes import RouteResult, build_route
from .store import DetectionStore

__all__ = ["app"]

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = correlation_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    store = DetectionStore(
        max_per_plate=settings.max_detections_per_plate,
        max_plates=settings.max_tracked_plates,
    )
    consumer = DetectionConsumer(settings.redis_url, settings.redis_detection_stream_key, store)
    consumer.start()

    registry = RegistryClient(
        settings.registry_base_url,
        settings.registry_timeout_s,
        settings.camera_location_cache_ttl_s,
    )

    app.state.settings = settings
    app.state.store = store
    app.state.consumer = consumer
    app.state.registry = registry

    try:
        yield
    finally:
        consumer.stop()
        await registry.aclose()


app = FastAPI(
    title="PRAHARI correlation service",
    version="0.1.0",
    summary="Cross-camera route reconstruction",
    lifespan=lifespan,
)


# --- dependencies ------------------------------------------------------------


def get_store(request: Request) -> DetectionStore:
    return request.app.state.store


def get_settings(request: Request) -> CorrelationSettings:
    return request.app.state.settings


def get_consumer(request: Request) -> DetectionConsumer:
    return request.app.state.consumer


def get_registry(request: Request) -> RegistryClient:
    return request.app.state.registry


StoreDep = Annotated[DetectionStore, Depends(get_store)]
SettingsDep = Annotated[CorrelationSettings, Depends(get_settings)]
ConsumerDep = Annotated[DetectionConsumer, Depends(get_consumer)]
RegistryDep = Annotated[RegistryClient, Depends(get_registry)]


# --- probes ------------------------------------------------------------------


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict:
    """Liveness. Deliberately does not touch Redis or the registry -- same
    reasoning as every other service's `/healthz` here: a liveness probe
    must never depend on something a mere reconnect, not a restart, would
    fix."""
    return {"status": "ok", "service": "correlation"}


@app.get("/readyz", tags=["ops"])
async def readyz(consumer: ConsumerDep, response: Response) -> dict:
    if not consumer.is_connected():
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "reason": "detection consumer not connected to redis"}
    return {"status": "ready"}


# --- routes --------------------------------------------------------------------


def _route_to_dict(result: RouteResult) -> dict:
    return {
        "plate": result.plate,
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
    plate: str, store: StoreDep, registry: RegistryDep, settings: SettingsDep
) -> dict:
    result = await build_route(
        plate, store, registry, settings.max_speed_kmh, settings.appearance_similarity_threshold
    )
    return _route_to_dict(result)
