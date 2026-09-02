"""Registry configuration. Environment only — no config files, no local state.

Every value here has a working default for `k3d` so that `make up` needs no
`.env`, except the two gateway credentials, which have no safe default and are
supplied as a Kubernetes Secret.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class RegistrySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PRAHARI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql://prahari:prahari@localhost:5432/prahari"

    db_pool_min: int = 2
    db_pool_max: int = 10

    # --- catalogue sync ------------------------------------------------------

    catalogue_source: str = "gujarat-sentinel-gateway"
    """Namespace for `external_id`. One registry can front several gateways and
    several direct-connect adapters; ids only have to be unique within a source."""

    sync_enabled: bool = True
    sync_interval_s: float = 300.0
    """Ids and the camera set change. Re-syncing on a timer is what makes the
    registry track the estate rather than describe it as it was at install."""

    sync_on_startup: bool = True

    # --- MediaMTX ------------------------------------------------------------

    mediamtx_api_url: str = "http://prahari-mediamtx:9997"
    mediamtx_reconcile: bool = True

    mediamtx_public_host: str = "prahari-mediamtx"
    """Host that appears in the fan-out URLs handed to consumers. The cluster
    service name locally; the ingress host in the cloud."""

    mediamtx_rtsp_port: int = 8554
    mediamtx_hls_port: int = 8888
    mediamtx_whep_port: int = 8889

    # --- health policy -------------------------------------------------------

    health_stale_after_s: int = 45
    """Default per-camera patience before "no heartbeat" becomes "unreachable".
    Roughly 4x the worker heartbeat interval, so one dropped report is not an
    outage."""

    health_fps_drift_ratio: float = 0.6
    health_fps_baseline_window_s: int = 3600
    health_fps_baseline_min_samples: int = 5

    health_black_frame_ratio: float = 0.9
    health_tamper_confirm_heartbeats: int = 3

    # --- retention -----------------------------------------------------------

    heartbeat_retention_days: int = 14
    """How long per-camera heartbeats are kept.

    TimescaleDB enforces this with a retention policy when the extension is
    present (migration 003). It is repeated here because the extension is not
    guaranteed — `postgis/postgis` does not carry it — and an unbounded
    heartbeat table is a slow leak that only shows up under the Day 5 load test,
    at 500 cameras x 6 rows/minute, which is exactly when we cannot afford it.
    """

    heartbeat_prune_interval_s: float = 3600.0

    # --- gap analysis --------------------------------------------------------

    gap_dark_zone_radius_m: float = 500.0
    """A non-working camera with no healthy camera inside this radius is a hole
    in coverage rather than a redundant unit. Urban default; a district with
    highway cameras will want it much larger, so it is a query parameter too."""


@lru_cache(maxsize=1)
def registry_settings() -> RegistrySettings:
    return RegistrySettings()
