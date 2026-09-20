"""BFF configuration. Environment only, same discipline as the registry's
`config.py` — every value has a working default for `k3d`, except the
bootstrap admin credentials, which have no safe default and are supplied as a
Kubernetes Secret only when standing up a cluster for the first time.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class BFFSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PRAHARI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Same Postgres as the registry — orgs/users/sessions/api_keys need
    # referential integrity with each other, so identity lives in one
    # database. See docs/ORG-TIERS-DESIGN.md §3.1.
    database_url: str = "postgresql://prahari:prahari@localhost:5432/prahari"
    db_pool_min: int = 2
    db_pool_max: int = 10

    # --- sessions --------------------------------------------------------

    session_cookie_name: str = "prahari_session"
    session_ttl_hours: int = 12

    session_cookie_secure: bool = True
    """False only for local `make dev` over plain HTTP. Every real profile's
    Helm values leaves this at the default — a session cookie sent over HTTP
    in the cloud is a credential handed to anything on the path."""

    login_rate_limit_attempts: int = 10
    login_rate_limit_window_s: float = 60.0
    """Sliding-window throttle on `/auth/login`, applied per-username and
    per-client-IP: beyond `login_rate_limit_attempts` attempts inside
    `login_rate_limit_window_s` seconds the endpoint answers 429. In-memory
    and per-process — enough to blunt a script against one pod without
    making login depend on Redis."""

    # --- registry ----------------------------------------------------------

    registry_base_url: str = "http://prahari-registry:8000"

    registry_internal_token: str = ""
    """Sent as `X-Internal-Token` on every registry call, once the BFF starts
    proxying camera reads in Stage 3. Must match `RegistrySettings.
    internal_token` on the other side — empty disables enforcement on both,
    which is the local/dev default; every real profile's chart sets a real
    shared value."""

    registry_timeout_s: float = 5.0

    state_root_org_path: str = "gj"
    """Used only for this service's own internal "which org owns camera X"
    lookups (the camera-detail 403 boundary and the SSE alert filter) — never
    as a caller's own request scope. Those two call sites need to see a
    camera regardless of who is asking, in order to then decide whether the
    *caller* may see it; querying the registry with the caller's own scope
    would just turn every out-of-scope camera into an unconditional 404, and
    the design explicitly wants 403 there instead. See docs/ORG-TIERS-DESIGN.
    md §4.2's gate test: "Zone-4 user requests a camera outside their subtree
    → 403"."""

    camera_org_cache_ttl_s: float = 300.0
    """How long a camera_id → org_path resolution is trusted before the BFF
    asks the registry again. Org reassignment is rare; asking on every alert
    on the SSE relay is not affordable."""

    # --- correlation ---------------------------------------------------------

    correlation_base_url: str = "http://prahari-correlation:8002"
    correlation_timeout_s: float = 10.0
    """Route reconstruction walks more rows than a camera lookup; the plate
    export endpoint (the mandatory submission path) gets a longer budget."""

    # --- audit -----------------------------------------------------------

    audit_db_path: str = "audit.db"
    """Append-only SQLite file for the hash-chained audit log — deliberately
    not the shared Postgres (docs/ORG-TIERS-DESIGN.md §4, mirroring DAY3-
    DESIGN.md §4.2's reasoning: single-writer, append-only, never joined).
    Stage 6's Helm chart mounts a PVC here; the bare filename is the
    local/dev default, resolved relative to the working directory."""

    # --- alerts ------------------------------------------------------------

    redis_url: str | None = None
    """`None` means the SSE relay never starts a consumer and `/api/v1/alerts
    /stream` reports 503 rather than hanging — the same "unset means honestly
    off" convention `prahari_correlation.config` uses for its own Redis
    setting."""

    alert_stream_key: str = "prahari:alerts"
    """Must match `MatchSettings.redis_stream_key` on the publishing side —
    one bus, two ends, same key."""

    sse_max_connections: int = 32
    """Cap on concurrent `/api/v1/alerts/stream` consumers — each holds an
    open request plus one Redis connection for the life of the tab, so an
    unbounded fan-out is a self-inflicted DoS. Beyond the cap the endpoint
    answers 429; the browser retries."""

    # --- bootstrap -----------------------------------------------------------

    bootstrap_admin_username: str | None = None
    bootstrap_admin_password: str | None = None
    bootstrap_admin_org_path: str = "gj"
    """Seeds exactly one admin user at startup, and only when both credentials
    are set AND the `users` table is still empty — the chicken-and-egg
    problem of the very first login, with no cluster shell and no CLI
    assumed. A no-op in every environment after the first user exists, so it
    is safe to leave configured; it is not safe to leave *unset* on a brand
    new deployment, since there would then be no way to create the first
    admin at all."""


@lru_cache(maxsize=1)
def bff_settings() -> BFFSettings:
    return BFFSettings()
