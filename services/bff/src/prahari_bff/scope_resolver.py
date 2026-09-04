"""Resolves which org owns a camera, for the two places this service needs
that fact without trusting the caller to supply it honestly: the
camera-detail 403 boundary (docs/ORG-TIERS-DESIGN.md §4.2's gate test 4) and
per-connection filtering on the SSE alert relay, where `Alert.detection.
camera_id` is the only per-alert scoping signal available (see
`proto/prahari/v1/events.proto`).

Not a general-purpose cache — narrowly, "which org path owns camera X",
refreshed on a TTL because org reassignment is rare and this must not cost a
registry round trip plus a Postgres query on every alert.
"""

from __future__ import annotations

import logging
import time

import asyncpg
import httpx

from .registry_client import RegistryClient
from .repository import org_path_for_id

log = logging.getLogger(__name__)

__all__ = ["CameraScopeResolver"]


class CameraScopeResolver:
    def __init__(
        self,
        registry: RegistryClient,
        pool: asyncpg.Pool,
        *,
        root_scope: str,
        ttl_s: float = 300.0,
    ) -> None:
        self._registry = registry
        self._pool = pool
        self._root_scope = root_scope
        self._ttl_s = ttl_s
        # camera_id -> (resolved_at, org_path). `org_path is None` (camera
        # not found, or has no org) is cached too, on the same TTL, so a
        # bogus camera id in a hot alert loop does not cost a lookup per
        # alert either.
        self._cache: dict[str, tuple[float, str | None]] = {}

    async def org_path_for_camera(self, camera_id: str) -> str | None:
        now = time.monotonic()
        cached = self._cache.get(camera_id)
        if cached is not None and now - cached[0] < self._ttl_s:
            return cached[1]
        org_path = await self._resolve(camera_id)
        self._cache[camera_id] = (now, org_path)
        return org_path

    async def _resolve(self, camera_id: str) -> str | None:
        try:
            response = await self._registry.get(
                f"/api/v1/cameras/{camera_id}", {"org_scope": self._root_scope}
            )
        except httpx.HTTPError:
            log.warning("registry unreachable resolving org for camera %s", camera_id)
            return None
        if response.status_code != 200:
            return None
        org_id = response.json().get("org_id")
        if org_id is None:
            return None
        return await org_path_for_id(self._pool, org_id)
