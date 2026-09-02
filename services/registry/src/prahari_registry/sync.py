"""Catalogue sync: `GET /api/ingest` → registry.

This is the "zero-code onboarding" claim made executable. One sync ingests the
whole estate; nothing is typed in, and nothing is hardcoded.

Three properties it must have, in order of how expensive getting them wrong is:

1. **Idempotent.** Running it twice changes nothing the second time. It runs on
   startup, on a timer, and whenever an operator presses the button during a
   demo.
2. **Tolerant of the camera set changing.** Ids rotate and cameras come and go.
   A camera that disappears is marked absent, never deleted — its detections are
   evidence.
3. **Never destructive of curated data.** The catalogue is authoritative for
   what it knows and silent about the rest; a district an operator typed in
   survives every future sync.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import asyncpg
import httpx
from prahari_common.catalogue import Catalogue, CatalogueClient
from prahari_common.config import GatewaySettings

from .config import RegistrySettings
from .mediamtx import MediaMTXClient
from .models import GeoPoint, SyncResult
from .repository import CameraRepository

log = logging.getLogger(__name__)

# Guards the sync itself, separately from the migration lock. Two replicas
# syncing at once would both be correct — the upserts are idempotent — but they
# would double the load on a shared government gateway for no benefit.
_SYNC_LOCK_ID = 0x5052_4148_5359_4E43  # "PRAHSYNC"


class CatalogueSync:
    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        repo: CameraRepository,
        settings: RegistrySettings,
        gateway: GatewaySettings | None = None,
        mediamtx: MediaMTXClient | None = None,
    ) -> None:
        self._pool = pool
        self._repo = repo
        self._s = settings
        self._gateway = gateway
        self._mediamtx = mediamtx or MediaMTXClient(settings)
        self._task: asyncio.Task | None = None

    # --- one pass ------------------------------------------------------------

    async def _fetch(self) -> Catalogue:
        """Fetch the live catalogue.

        `CatalogueClient` is synchronous (httpx.Client) and this service is
        async, so the blocking call goes to a thread rather than stalling the
        event loop and every in-flight heartbeat with it.
        """
        if self._gateway is None:
            raise RuntimeError(
                "gateway credentials are not configured: set PRAHARI_GATEWAY_HOST "
                "and PRAHARI_GATEWAY_PASSWORD (see .env.example)"
            )
        client = CatalogueClient(self._gateway)
        return await asyncio.to_thread(client.fetch)

    async def run_once(self, catalogue: Catalogue | None = None) -> SyncResult:
        """One sync pass. `catalogue` is injectable so the whole path can be
        exercised against a captured snapshot with no network."""
        started = datetime.now(UTC)
        result = SyncResult(source=self._s.catalogue_source, ok=False, started_at=started)
        run_id = await self._repo.start_sync_run(self._s.catalogue_source)

        try:
            if catalogue is None:
                catalogue = await self._fetch()

            result.cameras_seen = len(catalogue.cameras)
            result.codec_mix = catalogue.codec_mix()
            seen_ids: list[str] = []

            async with self._pool.acquire() as conn, conn.transaction():
                for entry in catalogue.cameras:
                    location = (
                        GeoPoint(latitude=entry.latitude, longitude=entry.longitude)
                        if entry.latitude is not None and entry.longitude is not None
                        else None
                    )
                    props = entry.properties
                    camera_id, inserted = await self._repo.upsert_from_catalogue(
                        conn,
                        source=self._s.catalogue_source,
                        external_id=entry.id,
                        site_name=entry.name or entry.location,
                        location=location,
                        codec=props.codec,
                        native_width=props.width,
                        native_height=props.height,
                        # Recorded, never used to derive time. See camera.proto.
                        declared_fps=props.declared_fps,
                        # From the catalogue where it supplied one, from the
                        # documented pattern only as a fallback — the catalogue
                        # is the contract, the URL pattern is not.
                        rtsp_url=entry.rtsp_url(self._gateway) if self._gateway else None,
                        hls_url=entry.hls_url(self._gateway) if self._gateway else None,
                        whep_url=entry.whep_url(self._gateway) if self._gateway else None,
                        catalogue_live=entry.live,
                        raw=entry.raw,
                        seen_at=started,
                    )
                    seen_ids.append(camera_id)
                    if inserted:
                        result.cameras_added += 1
                    else:
                        result.cameras_updated += 1

                result.cameras_absent = await self._repo.mark_absent(
                    conn, source=self._s.catalogue_source, seen_ids=seen_ids
                )

            result.ok = True
            log.info(
                "catalogue sync: %d seen, %d added, %d updated, %d absent, codecs=%s",
                result.cameras_seen,
                result.cameras_added,
                result.cameras_updated,
                result.cameras_absent,
                result.codec_mix,
            )
        except (httpx.HTTPError, ValueError, RuntimeError, asyncpg.PostgresError) as exc:
            # A failed sync must leave the previous state intact and the service
            # up: the estate the registry already knows about is still valid, and
            # a gateway blip is not a reason to stop tracking camera health.
            result.error = f"{type(exc).__name__}: {exc}"
            log.warning("catalogue sync failed: %s", result.error)
        finally:
            result.finished_at = datetime.now(UTC)
            await self._repo.finish_sync_run(run_id, result)

        if result.ok:
            await self._mediamtx.reconcile(await self._repo.desired_mediamtx_paths())

        return result

    async def run_once_locked(self) -> SyncResult | None:
        """Sync, unless another replica is already doing it. Returns None if so."""
        async with self._pool.acquire() as conn:
            acquired = await conn.fetchval("SELECT pg_try_advisory_lock($1)", _SYNC_LOCK_ID)
            if not acquired:
                log.info("catalogue sync already running elsewhere; skipping this pass")
                return None
            try:
                return await self.run_once()
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", _SYNC_LOCK_ID)

    # --- background loop -----------------------------------------------------

    async def _loop(self) -> None:
        if self._s.sync_on_startup:
            await self.run_once_locked()
        while True:
            await asyncio.sleep(self._s.sync_interval_s)
            try:
                await self.run_once_locked()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The loop outliving any single failure is the whole point of
                # having a loop. Logged with a traceback and retried next tick.
                log.exception("catalogue sync pass raised; continuing")

    def start(self) -> None:
        if not self._s.sync_enabled:
            log.info("catalogue sync disabled (PRAHARI_SYNC_ENABLED=false)")
            return
        if self._gateway is None:
            log.warning(
                "catalogue sync not started: gateway credentials absent. "
                "The registry is up and manual camera registration works; "
                "set PRAHARI_GATEWAY_HOST and PRAHARI_GATEWAY_PASSWORD to onboard the feed."
            )
            return
        self._task = asyncio.create_task(self._loop(), name="catalogue-sync")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._mediamtx.aclose()
