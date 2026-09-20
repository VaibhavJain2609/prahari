"""Trusted-caller HTTP client for the match engine.

Same shape and same trust model as `RegistryClient`: once the match engine's
own internal-token gate (`MatchSettings.internal_token`, env
`PRAHARI_MATCH_INTERNAL_TOKEN`) is set, its `/api/*` — watchlist summary and
reload, the recent-alerts buffer — is cluster-internal only, and the BFF is
the one thing on the browser's side of that gate allowed through it. Every
call carries `X-Internal-Token`, sourced from `BFFSettings.internal_token`:
one shared secret, two settings fields, because the env prefixes differ per
service.
"""

from __future__ import annotations

import logging

import httpx

from .config import BFFSettings

log = logging.getLogger(__name__)

__all__ = ["MatchEngineClient"]


class MatchEngineClient:
    def __init__(self, settings: BFFSettings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            base_url=settings.match_engine_base_url,
            timeout=settings.match_engine_timeout_s,
            headers={"X-Internal-Token": settings.internal_token},
        )

    async def get(self, path: str, params: dict | None = None) -> httpx.Response:
        return await self._client.get(path, params=params)

    async def post(self, path: str, json: dict | None = None) -> httpx.Response:
        return await self._client.post(path, json=json)

    async def aclose(self) -> None:
        await self._client.aclose()
