"""Endpoints and run parameters for a load-test run.

Everything is a flag or an env var; defaults match the port-forwards a local
k3d cluster exposes (`infra/k3d/cluster.yaml` maps 8554 for MediaMTX, the
registry/match-engine/correlation HTTP ports are reached via
`kubectl port-forward` — see README.md).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# District names the synthetic farm spreads cameras across. Real Gujarat
# districts; the registry treats `district` as a free-text attribute, so this
# is about making geojson/district-coverage queries do realistic work.
DISTRICTS = [
    "ahmedabad",
    "surat",
    "vadodara",
    "rajkot",
    "gandhinagar",
    "bhavnagar",
    "jamnagar",
    "junagadh",
    "kutch",
    "mehsana",
]

# Rough Gujarat bounding box for scattering camera locations.
LAT_MIN, LAT_MAX = 20.1, 24.7
LON_MIN, LON_MAX = 68.2, 74.5

INTERNAL_TOKEN_HEADER = "x-internal-token"


@dataclass
class Endpoints:
    """Where the platform under test is reachable from this host."""

    registry_url: str = "http://localhost:8000"
    match_grpc: str = "localhost:9001"
    match_http: str = "http://localhost:8001"
    correlation_url: str = "http://localhost:8002"
    internal_token: str = ""
    """Sent as `X-Internal-Token` on every internal call and as
    `x-internal-token` gRPC metadata. The harness impersonates the worker
    fleet — worker register/heartbeat on the registry and detections on the
    match engine — so its identity is `inference`: set the
    `inference-token` key's value, not the legacy `internal-token`. The
    envs resolve `PRAHARI_INFERENCE_TOKEN` first, then
    `PRAHARI_INTERNAL_TOKEN` as the shared-mode fallback for deployments
    that have not minted per-service keys."""

    mediamtx_publish_url: str = "rtsp://localhost:8554"
    """Where ffmpeg publishers push in `live` mode."""

    camera_rtsp_template: str = "rtsp://localhost:8554/lt-src/{i:05d}"
    """The URL stored on each registered camera, which the registry hands to
    MediaMTX as the pull source (`desired_mediamtx_paths` → reconcile). When
    MediaMTX runs in k3d this must resolve from *inside the cluster* —
    e.g. `rtsp://prahari-mediamtx:8554/lt-src/{i:05d}` so the restreamer pulls
    its own published `lt-src/*` paths."""

    @classmethod
    def from_env(cls) -> Endpoints:
        return cls(
            registry_url=os.environ.get("PRAHARI_REGISTRY_URL", cls.registry_url),
            match_grpc=os.environ.get("PRAHARI_MATCH_GRPC", cls.match_grpc),
            match_http=os.environ.get("PRAHARI_MATCH_HTTP", cls.match_http),
            correlation_url=os.environ.get("PRAHARI_CORRELATION_URL", cls.correlation_url),
            internal_token=os.environ.get("PRAHARI_INFERENCE_TOKEN")
            or os.environ.get("PRAHARI_INTERNAL_TOKEN", ""),
            mediamtx_publish_url=os.environ.get(
                "PRAHARI_MEDIAMTX_PUBLISH_URL", cls.mediamtx_publish_url
            ),
            camera_rtsp_template=os.environ.get(
                "PRAHARI_CAMERA_RTSP_TEMPLATE", cls.camera_rtsp_template
            ),
        )

    def headers(self) -> dict[str, str]:
        return {INTERNAL_TOKEN_HEADER: self.internal_token} if self.internal_token else {}


@dataclass
class RunConfig:
    """One stepped run. `cameras` is the step list — e.g. [5, 50, 500]."""

    mode: str = "simulate"  # simulate | live
    cameras: list[int] = field(default_factory=lambda: [5])
    duration_s: float = 60.0
    heartbeat_interval_s: float = 10.0
    detections_per_camera_s: float = 0.2
    watchlist_hit_rate: float = 0.01
    """Fraction of synthetic detections carrying a watchlist plate — drives the
    detection→alert latency probe. Watchlist plates are drawn from
    `data/watchlist/` so the match engine's real matcher produces real alerts."""
    watchlist_dir: str = "data/watchlist"
    grpc_streams: int = 4
    """Open StreamDetections client streams. One stream per worker is the real
    topology; a handful of streams lets one process drive a high message rate
    without pretending to be 500 processes."""
    alert_poll_s: float = 0.5
    sample_interval_s: float = 15.0
    streams_live: int = 0
    """`live` mode only: how many real ffmpeg→MediaMTX streams to publish.
    Capped by the host's decode capacity, not by the camera count."""
    cleanup: bool = True
    """Decommission seeded cameras at the end of the run. Soft-delete only —
    the registry never hard-deletes a camera, so a run leaves `decommissioned`
    rows behind, not dangling foreign keys."""
    label: str = ""
