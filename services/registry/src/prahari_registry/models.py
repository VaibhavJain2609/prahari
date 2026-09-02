"""API models. These mirror `proto/prahari/v1/camera.proto`.

The wire contract between services is protobuf; this is the REST/JSON face the
browser sees. Enum *values* are lowercase strings rather than the proto's
screaming-snake constants because they end up in URLs and in MapLibre style
expressions, but they map one-to-one and the mapping is asserted in the tests —
a divergence here would show up as a camera silently missing from the map.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class HealthState(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"
    TAMPERED = "tampered"


class CameraType(StrEnum):
    UNSPECIFIED = "unspecified"
    ANALOG = "analog"
    IP = "ip"
    PTZ = "ptz"
    ANPR = "anpr"


class Lifecycle(StrEnum):
    ACTIVE = "active"
    ABSENT = "absent"
    """In the catalogue once, not any more. Never deleted — its detections are
    evidence and evidence needs a camera to point at."""
    DECOMMISSIONED = "decommissioned"
    """Retired by an operator. Sticky: a stale gateway entry must not resurrect
    a camera somebody deliberately switched off."""


# The proto enum names, so a mapping error is a failing test and not a blank map.
PROTO_HEALTH_STATE = {
    HealthState.UNKNOWN: "HEALTH_STATE_UNSPECIFIED",
    HealthState.HEALTHY: "HEALTH_STATE_HEALTHY",
    HealthState.DEGRADED: "HEALTH_STATE_DEGRADED",
    HealthState.UNREACHABLE: "HEALTH_STATE_UNREACHABLE",
    HealthState.TAMPERED: "HEALTH_STATE_TAMPERED",
}

PROTO_CAMERA_TYPE = {
    CameraType.UNSPECIFIED: "CAMERA_TYPE_UNSPECIFIED",
    CameraType.ANALOG: "CAMERA_TYPE_ANALOG",
    CameraType.IP: "CAMERA_TYPE_IP",
    CameraType.PTZ: "CAMERA_TYPE_PTZ",
    CameraType.ANPR: "CAMERA_TYPE_ANPR",
}


class GeoPoint(BaseModel):
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)


class StreamEndpoints(BaseModel):
    rtsp_url: str | None = None
    hls_url: str | None = None
    whep_url: str | None = None
    """Browser preview only. Never an inference source — the WebRTC path loses
    the PTS fidelity evidence timestamps depend on."""

    fanout_rtsp_url: str | None = None
    """Where consumers should actually connect: our MediaMTX path, not the
    gateway. Every client gets its own copy of a source stream, so N workers
    pulling the gateway directly would exhaust a shared government feed."""

    fanout_hls_url: str | None = None
    fanout_whep_url: str | None = None


class CameraHealth(BaseModel):
    state: HealthState = HealthState.UNKNOWN
    reason: str | None = None
    last_heartbeat_at: datetime | None = None
    last_frame_at: datetime | None = None

    observed_fps: float | None = None
    """Measured from PTS. The declared rate is carried separately and is never
    compared against the clock."""

    declared_fps: float | None = None
    fps_drift: float | None = None
    """observed / declared, when both are known. Reporting only — a camera is
    judged degraded against its own history, not against the catalogue's claim."""

    black_frame_ratio: float | None = None
    tamper_suspected: bool = False
    consecutive_failures: int = 0
    loop_epoch: int = 0
    last_error: str | None = None

    @model_validator(mode="after")
    def _compute_drift(self) -> CameraHealth:
        if self.fps_drift is None and self.observed_fps and self.declared_fps:
            self.fps_drift = round(self.observed_fps / self.declared_fps, 3)
        return self


class Camera(BaseModel):
    id: str
    source: str
    external_id: str

    location: GeoPoint | None = None
    site_name: str | None = None
    district: str | None = None
    department: str | None = None
    owner: str | None = None

    camera_type: CameraType = CameraType.UNSPECIFIED
    vendor: str | None = None
    vms_platform: str | None = None
    codec: str | None = None
    native_width: int | None = None
    native_height: int | None = None

    endpoints: StreamEndpoints = Field(default_factory=StreamEndpoints)

    storage_location: str | None = None
    retention_days: int | None = None
    commissioned_at: datetime | None = None
    amc_expires_at: datetime | None = None

    lifecycle: Lifecycle = Lifecycle.ACTIVE
    catalogue_live: bool = True
    present_in_catalogue: bool = False
    last_seen_in_catalogue: datetime | None = None

    health: CameraHealth = Field(default_factory=CameraHealth)

    created_at: datetime | None = None
    updated_at: datetime | None = None


class CameraCreate(BaseModel):
    """Manual registration.

    Not every camera arrives through the gateway catalogue: Reference Model 2 is
    direct-connect, and a large part of the estate is analog behind a DVR that
    no catalogue enumerates. Those cameras are registered here, and from that
    point on they are indistinguishable to the rest of the platform — which is
    the entire claim of a vendor-neutral registry.
    """

    source: str = "manual"
    external_id: str
    location: GeoPoint | None = None
    site_name: str | None = None
    district: str | None = None
    department: str | None = None
    owner: str | None = None

    camera_type: CameraType = CameraType.UNSPECIFIED
    vendor: str | None = None
    vms_platform: str | None = None
    codec: str | None = None
    native_width: int | None = None
    native_height: int | None = None
    declared_fps: float | None = None

    rtsp_url: str | None = None
    hls_url: str | None = None
    whep_url: str | None = None

    storage_location: str | None = None
    retention_days: int | None = None
    commissioned_at: datetime | None = None
    amc_expires_at: datetime | None = None
    stale_after_s: int | None = None


class CameraUpdate(BaseModel):
    """Operator edit. Every field optional; only what is sent is written.

    Fields curated here are protected from being blanked by a later sync: the
    catalogue is authoritative for what it *knows*, not for what it omits.
    """

    location: GeoPoint | None = None
    site_name: str | None = None
    district: str | None = None
    department: str | None = None
    owner: str | None = None
    camera_type: CameraType | None = None
    vendor: str | None = None
    vms_platform: str | None = None
    storage_location: str | None = None
    retention_days: int | None = None
    commissioned_at: datetime | None = None
    amc_expires_at: datetime | None = None
    lifecycle: Lifecycle | None = None
    stale_after_s: int | None = None


class Heartbeat(BaseModel):
    """One health report from an ingest worker.

    Workers report observations; the registry decides state. Two workers on the
    same camera must not be able to disagree about whether it is up, and a
    worker cannot see that its own heartbeats have stopped arriving.
    """

    worker_id: str
    observed_at: datetime | None = None
    connected: bool = True

    measured_fps: float | None = None
    """From PTSClock.measured_fps. Null until enough frames have been seen to
    measure — that is not a fault, and must not read as one."""

    last_frame_at: datetime | None = None
    frames_decoded: int = 0
    consecutive_failures: int = 0
    black_frame_ratio: float | None = None
    tamper_suspected: bool = False
    loop_epoch: int = 0
    last_error: str | None = None


class HeartbeatAck(BaseModel):
    camera_id: str
    state: HealthState
    reason: str
    baseline_fps: float | None = None
    """The camera's own recent median delivery rate, which drift is judged
    against. Returned so a worker's logs explain a degraded verdict without a
    round trip to the database."""


class SyncResult(BaseModel):
    source: str
    ok: bool
    started_at: datetime
    finished_at: datetime | None = None
    cameras_seen: int = 0
    cameras_added: int = 0
    cameras_updated: int = 0
    cameras_absent: int = 0
    codec_mix: dict[str, int] = Field(default_factory=dict)
    error: str | None = None


class DistrictCoverage(BaseModel):
    district: str | None
    registered: int
    healthy: int
    degraded: int
    unreachable: int
    tampered: int
    unknown: int
    absent: int

    @property
    def working_ratio(self) -> float:
        return self.healthy / self.registered if self.registered else 0.0

    coverage_pct: float = 0.0


class DarkZone(BaseModel):
    """A camera that is down and has no healthy camera near enough to cover for
    it. This is the distinction the registry exists to make: a broken camera in
    a well-covered junction is a maintenance ticket, whereas a broken camera
    with nothing else in half a kilometre is a blind spot on a map."""

    camera_id: str
    site_name: str | None
    district: str | None
    location: GeoPoint | None
    state: HealthState
    reason: str | None
    nearest_healthy_m: float | None
    """Null means there is no healthy camera anywhere with a known location —
    strictly worse than a large number, and rendered as such."""


class NearestCamera(BaseModel):
    camera_id: str
    site_name: str | None
    district: str | None
    location: GeoPoint | None
    state: HealthState
    distance_m: float


class GeoJSONFeatureCollection(BaseModel):
    """Straight into MapLibre as a source. Kept server-side so the console does
    not reimplement the health overlay and drift from the API's version of it."""

    type: str = "FeatureCollection"
    features: list[dict[str, Any]] = Field(default_factory=list)
