"""Ingest-worker configuration. Everything comes from the environment.

Gateway connection settings (host, password, ports, TLS) live in
`prahari_common.config` because the registry reaches the same gateway. What
stays here is ingest *behaviour* — sampling rate, connection caps, backoff — which
is this service's business and nobody else's.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from prahari_common.config import GatewaySettings, gateway_settings
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "DetectorSettings",
    "GatewaySettings",
    "IngestSettings",
    "detector_settings",
    "gateway_settings",
    "ingest_settings",
]


class IngestSettings(BaseSettings):
    """Ingest behaviour. Mirrors the Helm `profile` values so the same knobs
    exist locally and in the cluster."""

    model_config = SettingsConfigDict(env_prefix="PRAHARI_INGEST_", extra="ignore")

    max_active_cameras: int = 5
    """§3: "Each connected client receives its own copy of the stream. Open only
    the cameras you are actively processing." A hard cap, not a suggestion —
    exceeding it degrades the shared government feed for everyone on it."""

    sample_fps: float = 2.0
    """Frames per second handed to the detector. Decode runs at full rate; this
    only gates inference."""

    backoff_initial_s: float = 2.0
    backoff_max_s: float = 30.0

    join_grace_s: float = 10.0
    """Window after connecting during which read failures are treated as the
    decoder waiting for its first IDR, not as a dead feed. §3: decoder messages
    at join "self-correct" and pipelines that abort on the first one "will
    bounce on those streams"."""

    read_failure_threshold: int = 30
    """Consecutive failed reads outside the grace window before declaring the
    connection dead. Inter-frame gaps must not trip this."""

    use_hls: bool = False
    """Pull over HLS instead of RTSP when deriving a URL from the catalogue.

    The documented fallback for a network where port 8554 is blocked, at the
    cost of several seconds of segment latency that the alerting path pays for
    directly — so it is a fallback, not a preference.

    It affects ONLY catalogue-derived URLs. A worker running in the cluster is
    handed an explicit MediaMTX fan-out URL by the registry and ignores this
    entirely, which is correct: a blocked upstream port is the restreamer's
    problem to absorb, once, rather than every worker's problem to route around.
    """

    assignment_refresh: bool = True
    """Re-read camera assignments on every heartbeat tick.

    Without this the worker's camera set is frozen at process start, and a
    camera the catalogue sync adds mid-run is invisible until the pod restarts.
    Switchable because a frozen set is the reproducible thing to want during a
    measured load run."""

    snapshot_dir: str = "data/catalogue"

    registry_url: str = "http://prahari-registry:8000"
    """Where health heartbeats go. The registry owns camera state; workers
    report observations and never decide health themselves — two workers on the
    same camera would otherwise disagree about whether it is up."""

    heartbeat_interval_s: float = 10.0

    liveness_file: str = "/tmp/prahari-worker-alive"
    """Touched after each heartbeat pass, and checked by the Kubernetes liveness
    probe. A worker serves no HTTP traffic, so there is no port to probe; without
    this, the failure that matters — the process alive but every pump thread
    dead — looks exactly like a healthy pod."""


@lru_cache(maxsize=1)
def ingest_settings() -> IngestSettings:
    return IngestSettings()


class DetectorSettings(BaseSettings):
    """Detection-cascade behaviour.

    INVARIANT: every `PRAHARI_DETECT_*` name the Helm chart sets must exist here
    as a field. `tests/test_detector_settings.py` parses
    `templates/inference.yaml` and asserts exactly that. A knob the chart writes
    and the code never reads is a profile switch that silently does not switch,
    which is worse than having no switch: the GPU profile looks applied and is
    not, and the number it was supposed to change ends up on a slide.

    The reverse direction is deliberately not asserted. A field with a sensible
    default that the chart does not override is fine.
    """

    model_config = SettingsConfigDict(env_prefix="PRAHARI_DETECT_", extra="ignore")

    model: str = "yolov8n.pt"
    """Vehicle detector weights. `yolov8n` on the laptop, a larger variant under
    the gpu profile — a value, never a code path."""

    decode_backend: Literal["cpu", "videotoolbox", "nvdec"] = "cpu"
    """Selected by profile, NEVER sniffed at runtime. A pipeline that probes for
    CUDA behaves differently on the laptop and the GPU node for reasons no
    values file records, which is precisely what the `profile` invariant exists
    to prevent."""

    batch_size: int = 4
    """Frames per inference call. Batched ACROSS cameras, not within one: a
    single camera at 2 fps cannot fill a batch without adding half a second of
    latency, while eight cameras fill one in tens of milliseconds."""

    batch_timeout_ms: float = 250.0
    """Flush a partial batch after this long. Without it a quiet estate holds
    frames indefinitely waiting for a batch that will never fill, and the
    end-to-end alert budget is missed by an unbounded margin."""

    motion_gate: bool = True
    """Drop frames with no significant motion before the detector runs. The main
    cost lever in the pipeline: on typical CCTV most sampled frames are static,
    and the fraction dropped here is the streams-per-GPU figure."""

    motion_threshold: float = 0.002
    """Fraction of the downscaled frame that must change to count as motion."""

    motion_warmup_frames: int = 3
    """Frames passed unconditionally after a background reset. The model is
    meaningless until it has seen a few frames, and a loop cut resets it on
    every camera, every cycle."""

    vehicle_confidence: float = 0.35
    plate_confidence: float = 0.25

    black_frame_luma: float = 16.0
    """Mean luma (0-255) below which a frame counts as black."""

    black_frame_ratio_alert: float = 0.9
    """Fraction of the recent window that must be black before the worker
    reports a black-frame condition."""

    tamper_blur_variance: float = 40.0
    """Laplacian variance below which the lens is treated as defocused or
    covered."""

    tamper_settle_frames: int = 15
    """Frames after a `loop_epoch` change during which scene-change tamper
    signals are suppressed. These feeds are recordings that loop, and the wrap
    is an abrupt scene cut on every camera, every cycle — a detector that does
    not whitelist it fires continuously and is switched off by the operator on
    the first day, which is the same as not having one."""

    match_engine_grpc: str = "prahari-match-engine:9001"
    """Where `MetadataIngestService` lives. One long-lived client stream per
    worker: at statewide rates this link carries millions of small messages, and
    per-message framing overhead is why it is gRPC and not JSON over HTTP/1.1."""

    publish_enabled: bool = True
    """Off for offline measurement runs, where the cascade is being profiled and
    a match engine is not deployed."""


@lru_cache(maxsize=1)
def detector_settings() -> DetectorSettings:
    return DetectorSettings()
