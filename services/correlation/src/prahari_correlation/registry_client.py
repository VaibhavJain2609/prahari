"""A thin read-only client onto `services/registry`'s REST surface. The
registry is the source of truth for camera location (CLAUDE.md) --
correlation never duplicates it, it caches the answer for a short TTL so
feasibility gating (DAY3-DESIGN.md §3.2) does not make one HTTP round trip
per hop on every route request.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

__all__ = ["GeoPoint", "DarkZone", "RegistryClient"]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GeoPoint:
    latitude: float
    longitude: float


@dataclass(frozen=True)
class DarkZone:
    camera_id: str
    location: GeoPoint | None


class RegistryClient:
    def __init__(
        self,
        base_url: str,
        timeout_s: float,
        cache_ttl_s: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._cache_ttl_s = cache_ttl_s
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout_s)
        # camera_id -> (cached_at_monotonic, location). A miss (camera has no
        # location, or the lookup failed) is cached too -- otherwise a
        # location-less camera costs one HTTP round trip on every hop for the
        # life of the process.
        self._location_cache: dict[str, tuple[float, GeoPoint | None]] = {}

    async def camera_location(self, camera_id: str) -> GeoPoint | None:
        cached = self._location_cache.get(camera_id)
        now = time.monotonic()
        if cached is not None and now - cached[0] < self._cache_ttl_s:
            return cached[1]

        location = await self._fetch_camera_location(camera_id)
        self._location_cache[camera_id] = (now, location)
        return location

    async def _fetch_camera_location(self, camera_id: str) -> GeoPoint | None:
        try:
            response = await self._client.get(f"/api/v1/cameras/{camera_id}")
            response.raise_for_status()
        except httpx.HTTPError:
            log.exception("failed to fetch camera %s from registry", camera_id)
            return None

        payload = response.json()
        location = payload.get("location")
        if not location:
            return None
        return GeoPoint(latitude=location["latitude"], longitude=location["longitude"])

    async def dark_zones(self) -> list[DarkZone]:
        """Cameras the registry's gap analysis reports as down with nothing
        healthy nearby -- surfaced between route hops (DAY3-DESIGN.md §3.4)
        rather than silently interpolated across. Not cached: this is called
        once per route request, not once per hop, so the TTL cost/benefit
        that justifies caching `camera_location` does not apply here."""
        try:
            response = await self._client.get("/api/v1/gaps/dark-zones")
            response.raise_for_status()
        except httpx.HTTPError:
            log.exception("failed to fetch dark zones from registry")
            return []

        zones = []
        for entry in response.json():
            location = entry.get("location")
            zones.append(
                DarkZone(
                    camera_id=entry["camera_id"],
                    location=GeoPoint(
                        latitude=location["latitude"], longitude=location["longitude"]
                    )
                    if location
                    else None,
                )
            )
        return zones

    async def aclose(self) -> None:
        await self._client.aclose()
