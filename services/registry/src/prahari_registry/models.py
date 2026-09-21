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


class OrgKind(StrEnum):
    STATE = "state"
    ORGANIZATION = "organization"
    LOCAL_BODY = "local_body"
    """A label on the node, not a structural constraint — the tree is
    arbitrary-depth, and nothing stops a `local_body` from parenting another
    `local_body`. `kind` is for the console to pick a board, not for the
    scope predicate, which only ever looks at `path`."""


class Org(BaseModel):
    id: str
    parent_id: str | None = None
    path: str
    """Dotted ltree label, e.g. `gj.ahmedabad_city.zone_4`. A camera or a user
    is in scope for a principal at path P iff its own org's path is `<@ P`
    (P and everything under it)."""
    kind: OrgKind
    name: str
    created_at: datetime | None = None


class OrgCreate(BaseModel):
    parent_id: str | None = None
    """None only for the root — the seeded state node already exists, so in
    practice this is always set. Enforced at the repository, not here: a
    Pydantic model has no query access to check the parent exists."""
    label: str = Field(pattern=r"^[a-z0-9_]+$")
    """One ltree segment, appended to the parent's path. Lowercase and
    underscores only — ltree's own label alphabet — so a label round-trips
    through `path <@` without escaping."""
    kind: OrgKind
    name: str


class GeoPoint(BaseModel):
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)


class StreamEndpoints(BaseModel):
    """MediaMTX fan-out URLs — the *internal* half of the endpoints contract.

    Upstream URLs (`cameras.rtsp_url`/`hls_url`/`whep_url`, which for catalogue
    cameras are the government gateway's pull URLs) are deliberately absent
    from this model: they are stored columns read only by
    `CameraRepository.desired_mediamtx_paths` for the reconcile path, and no
    HTTP response may carry them — a response is the difference between
    "stored for the restreamer" and "handed to whoever can read the API".

    The fan-out URLs below are worker-facing. `GET /api/v1/cameras` doubles as
    the inference workers' assignments feed (`worker.py` prefers
    `fanout_rtsp_url`), and with MediaMTX auth armed the RTSP/HLS URLs embed
    the internal reader credential. That is safe only because this service's
    `/api/*` is cluster-internal (`internal_token` gate) and the BFF strips
    `endpoints` entirely before a payload approaches a browser — see
    `prahari_bff.app._public_camera`.
    """

    fanout_rtsp_url: str | None = None
    """Where consumers should actually connect: our MediaMTX path, not the
    gateway. Every client gets its own copy of a source stream, so N workers
    pulling the gateway directly would exhaust a shared government feed."""

    fanout_hls_url: str | None = None

    fanout_whep_url: str | None = None
    """WHEP is a browser-only protocol and browsers never see this object —
    they get a short-lived ticket from the BFF's preview-ticket endpoint.
    Carried here for completeness (and so a future internal consumer can
    tell the path exists), but it never embeds credentials."""


class CameraPreview(BaseModel):
    """Whether a live stream exists behind this camera — the only part of the
    endpoints contract a browser-facing response is allowed to say.

    The browser learns *that* a preview can be minted here; *how* to reach it
    comes from `POST /api/v1/media/preview-ticket` on the BFF, which is the
    audited path — a URL in a catalogue row is an access that no audit entry
    could ever be written for."""

    available: bool = False
    """True when the camera has an upstream stream MediaMTX can pull (today:
    a stored `rtsp_url`, the same predicate `desired_mediamtx_paths` uses).
    Says nothing about health — a degraded camera still has a previewable
    stream."""


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

    org_id: str | None = None
    """The org-tree scope this camera is visible under. Nullable only in the
    type — the column is NOT NULL from migration 005 onward, so this is None
    solely as defensive typing against a row the model has not yet learned
    every field of, never as an observed state."""
    adapter: str = "manual"
    """Which `CameraAdapterService` implementation fronts this camera:
    gateway | rtsp-direct | onvif | manual. See proto/prahari/v1/adapter.proto."""

    camera_type: CameraType = CameraType.UNSPECIFIED
    vendor: str | None = None
    vms_platform: str | None = None
    codec: str | None = None
    native_width: int | None = None
    native_height: int | None = None

    endpoints: StreamEndpoints = Field(default_factory=StreamEndpoints)
    """Worker-facing fan-out URLs (see `StreamEndpoints`). Empty object when
    the camera has no pullable upstream. Never contains upstream gateway URLs
    and must never reach a browser — the BFF drops this field in
    `_public_camera`."""

    preview: CameraPreview = Field(default_factory=CameraPreview)
    """Capability flag safe to show anyone who can see the camera itself:
    whether a preview *could* be minted, not how to reach the stream."""

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

    org_id: str | None = None
    """Which org this camera belongs to. Left optional here because Stage 1
    has no principal yet to force it from — the repository falls back to
    `RegistrySettings.sync_default_org_path`. Stage 2 (BFF) makes this
    non-optional in practice by always supplying the caller's own org_id, the
    same way an operator is never trusted to type their own scope."""
    adapter: str = "manual"

    stream_username: str | None = None
    stream_password: str | None = None
    """Credentials for `rtsp_url`, when the DVR/NVR requires auth. Encrypted
    (AES-GCM, `crypto.py`) into `cameras.stream_secret` on write; never
    stored or echoed as plaintext, and absent from every response model —
    `Camera` has no field for either. Decrypted only server-side, when
    building the upstream URL MediaMTX pulls from."""

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
    org_id: str | None = None
    """Reassignment — e.g. a local body claiming a camera the catalogue sync
    placed at the state root. Like every other field here, sent only when
    changing it; omitted means untouched."""
    stream_username: str | None = None
    stream_password: str | None = None
    """Same handling as `CameraCreate.stream_password` — encrypted into
    `stream_secret` on write, never read back. Sending `stream_username`
    without `stream_password` (or vice versa) updates only the field sent;
    the repository does not require them together, since rotating just the
    password is the common case."""
    camera_type: CameraType | None = None
    vendor: str | None = None
    vms_platform: str | None = None
    storage_location: str | None = None
    retention_days: int | None = None
    commissioned_at: datetime | None = None
    amc_expires_at: datetime | None = None
    lifecycle: Lifecycle | None = None
    stale_after_s: int | None = None


class CameraProbeRequest(BaseModel):
    """Stage 4c — a connectivity check for an operator filling in the manual
    registration form, before anything is saved. `rtsp_url` is required;
    `username`/`password` are optional and used only for this one probe
    request — they are never persisted here (registration, separately,
    persists them encrypted via `CameraCreate.stream_password`)."""

    rtsp_url: str
    username: str | None = None
    password: str | None = None


class Heartbeat(BaseModel):
    """One health report from an ingest worker.

    Workers report observations; the registry decides state. Two workers on the
    same camera must not be able to disagree about whether it is up, and a
    worker cannot see that its own heartbeats have stopped arriving.
    """

    worker_id: str
    observed_at: datetime | None = None
    connected: bool = True

    measured_fps: float | None = Field(default=None, ge=0)
    """From PTSClock.measured_fps. Null until enough frames have been seen to
    measure — that is not a fault, and must not read as one. Bounded below at
    zero: a negative rate is a bug in the reporter, and letting it through would
    corrupt the drift baseline it gets folded into."""

    last_frame_at: datetime | None = None
    frames_decoded: int = Field(default=0, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)
    black_frame_ratio: float | None = Field(default=None, ge=0, le=1)
    """A ratio, so [0, 1]. Anything outside that range is a broken reporter,
    not a camera that is somehow more than entirely black."""
    tamper_suspected: bool = False
    loop_epoch: int = Field(default=0, ge=0)
    last_error: str | None = None


class HeartbeatAck(BaseModel):
    camera_id: str
    state: HealthState
    reason: str
    baseline_fps: float | None = None
    """The camera's own recent median delivery rate, which drift is judged
    against. Returned so a worker's logs explain a degraded verdict without a
    round trip to the database."""


class HeartbeatSample(BaseModel):
    """One stored heartbeat, as served by `GET /cameras/{id}/health-history`.

    This is the raw observation the worker sent — deliberately NOT the derived
    verdict (`health.py` derives state at read time, and the detail drawer
    wants to show the observations the verdicts were computed from). Column set
    mirrors `camera_heartbeat` (migration 003); there is no surrogate id because
    the table has none — `(camera_id, observed_at)` is the identity.
    """

    observed_at: datetime
    worker_id: str
    connected: bool
    measured_fps: float | None = None
    last_frame_at: datetime | None = None
    frames_decoded: int = 0
    consecutive_failures: int = 0
    black_frame_ratio: float | None = None
    tamper_suspected: bool = False
    loop_epoch: int = 0
    last_error: str | None = None


class WorkerRegister(BaseModel):
    """An ingest worker announcing itself to the sharding pool.

    `worker_id` is caller-supplied — the pod name under Kubernetes, the
    hostname otherwise — and is the same identity the worker stamps on every
    camera heartbeat, so "which pod owned this camera" is one column, not a
    log correlation exercise. Registration is idempotent: re-registering is
    the keep-alive and refreshes `last_seen`.
    """

    worker_id: str = Field(min_length=1, max_length=253)
    rotate_secret: bool = False
    """Ask the registry for a worker secret now. Dual-purpose:

    * On an UNBOUND worker_id (`workers.secret_hash` NULL) it is the opt-in
      bind — the registry mints a secret, stores its digest, and returns the
      plaintext once. Workers that never send the flag stay unbound, which is
      what keeps pre-binding callers working: a secret a worker never asked
      for is a credential it cannot present on its next call.
    * On a BOUND worker_id it is rotation — a fresh secret replaces the
      stored digest — and the CURRENT secret must be presented as
      `X-Worker-Secret`, so a stolen secret cannot be used to re-key the
      identity it opens.
    """


class WorkerRegistration(BaseModel):
    """What a worker learns from registering: its shard coordinates and how
    long the lease it just refreshed lives.

    `shard_index`/`shard_count` are modulo coordinates over the fleet's alive
    set (workers whose `last_seen` is within 2x the lease, ordered by
    worker_id). They are advisory, not a lock — membership changes as pods
    come and go, and a camera briefly claimed by two workers during a
    reshard is tolerated: both read the same MediaMTX fan-out path, which
    holds one upstream pull regardless of reader count.
    """

    worker_id: str
    shard_index: int = Field(ge=0)
    shard_count: int = Field(ge=1)
    lease_s: int = Field(ge=1)

    worker_secret: str | None = None
    """The minted per-worker secret — populated ONLY on the response that
    created the binding (the opt-in mint) or rotated it, never on a plain
    keep-alive: the plaintext crosses the wire exactly once and only its
    SHA-256 digest is stored. The worker presents it as `X-Worker-Secret`
    on every subsequent worker-facing call (re-register, assignments,
    heartbeats); a bound worker_id without it is refused."""


class WorkerAssignment(WorkerRegistration):
    """A registration plus the camera slice it entitles the worker to pull.

    `cameras` carries the same `Camera` shape as `GET /api/v1/cameras` —
    including the MediaMTX fan-out endpoints — so the worker builds its
    `CameraAssignment`s identically whichever endpoint served them.
    """

    cameras: list[Camera] = Field(default_factory=list)


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
