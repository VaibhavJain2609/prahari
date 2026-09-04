"""Trusted-caller HTTP client for the registry.

Once `RegistrySettings.internal_token` is set, the registry's `/api/*` is
cluster-internal only (see `services/registry/src/prahari_registry/app.py`'s
`require_internal_token` middleware) — this is the one thing on the other
side of that gate that the BFF is allowed to be. Every call carries
`X-Internal-Token`; nothing here decides *what* scope to ask for, that is the
caller's job (see `app.py`'s `_scoped_params` for the browser-facing routes,
and `CameraScopeResolver` for the root-scoped internal lookups).
"""

from __future__ import annotations

import logging

import httpx

from .config import BFFSettings

log = logging.getLogger(__name__)

__all__ = ["RegistryClient"]


class RegistryClient:
    def __init__(self, settings: BFFSettings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            base_url=settings.registry_base_url,
            timeout=settings.registry_timeout_s,
            headers={"X-Internal-Token": settings.registry_internal_token},
        )

    async def get(self, path: str, params: dict | None = None) -> httpx.Response:
        return await self._client.get(path, params=params)

    async def aclose(self) -> None:
        await self._client.aclose()
