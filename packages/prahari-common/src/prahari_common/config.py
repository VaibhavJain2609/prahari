"""Gateway connection settings, shared by every service that talks upstream.

The gateway host and access password are credentials for a government feed.
They are never defaulted to a real value, never written to a file the repo
tracks, and never logged. `.env` is gitignored; `.env.example` shows the shape.

This lives in the shared package rather than in one service because the ingest
workers and the registry both reach the same gateway, and two copies of the
connection rules would drift the moment one of them is corrected against a live
200 response.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewaySettings(BaseSettings):
    """Connection settings for the camera gateway."""

    model_config = SettingsConfigDict(
        env_prefix="PRAHARI_GATEWAY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = Field(
        description="Gateway hostname, no scheme. Supplied at deploy time; the "
        "repo never carries the real value."
    )
    password: SecretStr = Field(
        description="Access password. SecretStr so it cannot land in a log line "
        "or a traceback through an accidental f-string."
    )

    rtsp_port: int = 8554
    whep_port: int = 8889

    scheme: str = "https"
    """The gateway is reachable over TLS. RTSP is separate and unencrypted on
    8554 — that is the gateway's design, not ours, and is a point to raise in
    SECURITY.md rather than to work around."""

    verify_tls: bool = True

    catalogue_path: str = "/api/ingest"

    request_timeout_s: float = 15.0

    @field_validator("host")
    @classmethod
    def _reject_scheme_in_host(cls, v: str) -> str:
        # Pasting the full URL into the host var is the obvious mistake, and it
        # produces a confusing "https://https://..." far from the cause.
        if "://" in v:
            raise ValueError(
                "host must be a bare hostname without a scheme "
                "(set PRAHARI_GATEWAY_SCHEME separately)"
            )
        return v.rstrip("/")

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://{self.host}"

    @property
    def catalogue_url(self) -> str:
        return f"{self.base_url}{self.catalogue_path}"


@lru_cache(maxsize=1)
def gateway_settings() -> GatewaySettings:
    return GatewaySettings()  # type: ignore[call-arg]
