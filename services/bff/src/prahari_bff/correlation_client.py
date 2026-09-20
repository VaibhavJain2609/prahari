"""Client for the correlation service's route reconstruction endpoint — the
mandatory submission path (plate → timestamped, location-wise route),
reached through the BFF so it is authenticated, purpose-coded and audited
rather than open to anyone who can route to the cluster.

Deliberately thin: correlation already returns exactly the shape
`docs/DAY3-DESIGN.md`'s export spec needs
(`{plate, hops: [...], rejected: [...], dark_zones: [...]}`), so this client
does no reshaping — `export.py` reads the dict directly.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from .config import BFFSettings

__all__ = ["CorrelationClient"]


class CorrelationClient:
    def __init__(self, settings: BFFSettings, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=settings.correlation_base_url,
            timeout=settings.correlation_timeout_s,
        )

    async def get_route(self, plate: str) -> dict:
        # The plate is caller-supplied path material — quote it so a value
        # like "../admin" or "X?drop=1" can't escape or mutate the route.
        response = await self._client.get(f"/api/v1/routes/{quote(plate, safe='')}")
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()
