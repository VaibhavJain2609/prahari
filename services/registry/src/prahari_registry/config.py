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

    sync_default_org_path: str = "gj"
    """The org a synced camera lands in on first insert (`orgs.path`, an ltree
    label — see migrations/005_orgs.sql, which seeds this exact root). Applied
    on INSERT only: once a local body reassigns a synced camera to itself, no
    future sync moves it back, the same way district/department/owner already
    survive a sync once an operator has curated them."""

    # --- MediaMTX ------------------------------------------------------------

    mediamtx_api_url: str = "http://prahari-mediamtx:9997"
    mediamtx_reconcile: bool = True

    mediamtx_public_host: str = "prahari-mediamtx"
    """Host that appears in the fan-out URLs handed to consumers. The cluster
    service name locally; the ingress host in the cloud."""

    mediamtx_rtsp_port: int = 8554
    mediamtx_hls_port: int = 8888
    mediamtx_whep_port: int = 8889

    # --- connectivity probe ----------------------------------------------------

    probe_allowed_ports: set[int] = {554}
    """Ports `/api/v1/cameras/probe` may connect to (`PRAHARI_PROBE_ALLOWED_PORTS`,
    JSON list, e.g. `[554, 8554]`). The probe is a server-side connect to a
    caller-supplied host — SSRF — so the port list defaults closed at the RTSP
    well-known port rather than open. Anything not listed is refused with a 400
    before DNS is even consulted."""

    # --- access ----------------------------------------------------------

    internal_token: str = ""
    """Required as `X-Internal-Token` on every `/api/*` request when set —
    see docs/ORG-TIERS-DESIGN.md §3.3. This is what makes the BFF's org-scope
    check meaningful rather than decorative: a scope predicate enforced only
    at the BFF is worth nothing if the registry it fronts is still reachable
    directly. Must match `BFFSettings.registry_internal_token` (and every
    other internal caller's own copy) exactly.

    Empty disables enforcement — the local/dev default, since k3d has no
    ingress separating "browser-reachable" from "cluster-internal" the way a
    real profile's NetworkPolicy does. Every real profile's chart sets a real
    shared value; leaving it empty in the cloud profile would be the same
    silent-no-op failure `CLAUDE.md`'s hard invariant on `PRAHARI_*` env
    already warns about."""

    credential_key: str = ""
    """32-byte AES-256 key, urlsafe-base64-encoded, for `cameras.stream_secret`
    (see `crypto.py`). Empty by default — only raises (`CredentialKeyError`)
    the moment a locally-registered camera's credential is actually written
    or read, never at settings-parse time, so a deployment with no analog
    cameras carrying credentials never needs this configured. Every real
    profile that onboards analog/DVR cameras must set a real value."""

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
