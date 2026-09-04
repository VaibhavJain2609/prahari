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

    # --- registry ----------------------------------------------------------

    registry_base_url: str = "http://prahari-registry:8000"

    registry_internal_token: str = ""
    """Sent as `X-Internal-Token` on every registry call, once the BFF starts
    proxying camera reads in Stage 3. Must match `RegistrySettings.
    internal_token` on the other side — empty disables enforcement on both,
    which is the local/dev default; every real profile's chart sets a real
    shared value."""

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
