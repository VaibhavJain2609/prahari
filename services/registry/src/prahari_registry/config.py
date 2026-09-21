"""Registry configuration. Environment only — no config files, no local state.

Every value here has a working default for `k3d` so that `make up` needs no
`.env`, except the two gateway credentials, which have no safe default and are
supplied as a Kubernetes Secret.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from prahari_common.internal_auth import parse_caller_tokens
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Caller names the registry accepts in isolated mode — the BFF (camera/org
# reads and writes), correlation (camera-location lookups) and the ingest
# workers (register/heartbeat/assignments). "internal" — the shared
# `internal_token` identity — is additionally accepted on every service as
# the backward-compat path; see prahari_common.internal_auth.caller_accepted.
ACCEPTED_CALLERS = frozenset({"bff", "correlation", "inference"})


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

    # --- MediaMTX auth callback ------------------------------------------------
    # The restreamer runs `authMethod: http` and POSTs every credential check
    # to this service's /api/v1/mediamtx/auth (see media_auth.py). Browser
    # preview tickets are Ed25519 JWTs minted by the BFF; this service verifies
    # them against the BFF's public JWKS, fetched on demand and cached.

    media_auth_jwks_url: str = "http://prahari-bff:8080/api/v1/media/jwks"
    """Where the BFF publishes the public half of its media-ticket keypair.
    Unreachable simply means no preview tickets can be verified — machine
    credentials (worker reads, the reconcile API) are checked locally against
    `internal_token` and are unaffected."""

    media_auth_jwks_ttl_s: float = 300.0
    """How long a fetched JWKS is trusted before re-pulling. Bounds how fast a
    rotated BFF keypair (ephemeral when PRAHARI_MEDIA_JWT_PRIVATE_KEY is unset)
    takes effect here; a ticket minted under a key this cache has not seen is
    refused, never silently accepted."""

    media_auth_jwks_timeout_s: float = 5.0

    media_auth_rate_limit_attempts: int = 20
    """Sliding-window cap on `/api/v1/mediamtx/auth` calls per source IP per
    `media_auth_rate_limit_window_s`. The endpoint is unauthenticated by
    necessity (MediaMTX cannot hold the secret it asks about), which makes it
    a password-equality oracle — the window bounds guess attempts. It must
    stay above the expected auth-callback rate: every new MediaMTX session
    (worker RTSP connect, browser WHEP POST, reconcile API call) costs one
    call, all from the restreamer's pod IP, so a reconnect storm across the
    whole fleet can briefly deny legitimate reads — MediaMTX treats a 429 as
    a refusal and the worker retries on its own backoff."""

    media_auth_rate_limit_window_s: float = 60.0

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
    already warns about.

    When `internal_tokens` is configured this value keeps working as the
    `"internal"` caller identity — the backward-compat credential accepted
    on every service for anything still holding the shared token."""

    internal_tokens: Annotated[dict[str, str], NoDecode] = {}
    """Caller-name → token map for per-service isolation
    (`PRAHARI_INTERNAL_TOKENS`, JSON `{"bff": "..."}` or the comma form
    `bff:tok,correlation:tok2`). When non-empty the gate runs "isolated": a
    presented token must resolve to a caller in `ACCEPTED_CALLERS` rather
    than merely match the shared value — so a leaked worker token no longer
    unlocks every service. Empty (plus `caller_token_*` unset) means the
    legacy shared-token check on `internal_token`, which is what keeps
    pre-isolation deployments working unchanged.

    `NoDecode` keeps pydantic-settings from JSON-decoding the env value
    itself, so the field validator sees the raw string and can accept the
    comma form as well as JSON."""

    caller_token_bff: str = ""
    caller_token_correlation: str = ""
    caller_token_inference: str = ""
    """The chart's delivery mechanism for `internal_tokens` — Kubernetes env
    expansion cannot assemble a JSON map from several `secretKeyRef`s into
    one var, so the chart emits one `PRAHARI_CALLER_TOKEN_<NAME>` per caller
    (`prahari-internal` keys `bff-token`, `correlation-token`,
    `inference-token`) and the model validator below merges them into
    `internal_tokens`. A `caller_token_*` entry wins over the same name in
    `internal_tokens` — the specific env beats the aggregate one."""

    @field_validator("internal_tokens", mode="before")
    @classmethod
    def _parse_internal_tokens(cls, value: object) -> dict[str, str]:
        return parse_caller_tokens(value)

    @model_validator(mode="after")
    def _merge_caller_tokens(self) -> RegistrySettings:
        merged = dict(self.internal_tokens)
        for name in ("bff", "correlation", "inference"):
            token = getattr(self, f"caller_token_{name}")
            if token:
                merged[name] = token
        self.internal_tokens = merged
        return self

    worker_media_token: str = ""
    """The MediaMTX reader credential embedded in fan-out URLs as
    `worker:<worker-media-token>` userinfo (`mediamtx.fanout_endpoints`,
    checked by `media_auth.authorize`).

    Deliberately a SEPARATE secret from `internal_token`: the media credential
    travels inside URLs handed to every inference pod, so it is the token most
    exposed to leaks — coupling it to the internal API token would make a
    leaked pull URL an internal-API credential as well, and rotating either
    would force rotating both. `worker:` auth fails closed when this is empty:
    a credential that cannot be checked cannot be granted."""

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

    # --- worker assignment ---------------------------------------------------

    assignment_lease_s: int = 60
    """How long a registered inference worker's membership lasts without a
    re-register (`POST /api/v1/workers/register` doubles as the keep-alive).

    Two windows derive from it: a worker counts toward `shard_count` while its
    `last_seen` is within 2x this lease, and its `workers` row is reaped once
    stale for 3x — the first shrinks the pool promptly when a pod dies, the
    second is just table hygiene. The chart sets it well above the worker's
    own refresh cadence (`PRAHARI_INGEST_ASSIGNMENT_REFRESH_S`) so one missed
    refresh does not eject a live pod from the pool."""

    worker_secret_required: bool = False
    """Refuse worker registrations that would leave the worker_id UNBOUND
    (`PRAHARI_WORKER_SECRET_REQUIRED`, migration 010).

    With per-worker secrets, `X-Internal-Token` alone no longer suffices to
    claim a bound worker_id — but a token holder can still mint NEW unbound
    ids (and steal a not-yet-bound one) while this is off. Arming it refuses
    any register that does not ask for a secret, closing the last unbound
    path: flip it only once every ingest worker runs a build that sends
    `rotate_secret` on first register — a pre-binding worker would be
    locked out of the pool entirely, which fails closed into dropped
    cameras rather than open into spoofed heartbeats."""

    # --- gap analysis --------------------------------------------------------

    gap_dark_zone_radius_m: float = 500.0
    """A non-working camera with no healthy camera inside this radius is a hole
    in coverage rather than a redundant unit. Urban default; a district with
    highway cameras will want it much larger, so it is a query parameter too."""


@lru_cache(maxsize=1)
def registry_settings() -> RegistrySettings:
    return RegistrySettings()
