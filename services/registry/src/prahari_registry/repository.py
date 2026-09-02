"""SQL. Every statement the registry runs lives here.

Written against asyncpg with explicit SQL rather than an ORM. PostGIS geography
columns, `ON CONFLICT` upserts and KNN nearest-neighbour queries are all things
an ORM makes harder to read, and the spatial queries are the part of this
service most worth being able to read.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import asyncpg

from .config import RegistrySettings
from .health import HealthVerdict
from .mediamtx import fanout_endpoints
from .models import (
    Camera,
    CameraCreate,
    CameraHealth,
    CameraType,
    CameraUpdate,
    GeoPoint,
    HealthState,
    Heartbeat,
    Lifecycle,
    StreamEndpoints,
    SyncResult,
)

log = logging.getLogger(__name__)


def _point(location: GeoPoint | None) -> str | None:
    """WKT for a geography(Point, 4326). Longitude first — the commonest way to
    silently put every camera in the wrong hemisphere."""
    if location is None:
        return None
    return f"SRID=4326;POINT({location.longitude} {location.latitude})"


def camera_from_row(row: asyncpg.Record, settings: RegistrySettings) -> Camera:
    """Map a `camera_current` row onto the API model.

    Reads `effective_health_state`, not `health_state`: the view has already
    applied the staleness overlay, and the raw column is the last verdict a
    heartbeat produced, which for a camera that went dark an hour ago still says
    "healthy".
    """
    location = (
        GeoPoint(latitude=row["latitude"], longitude=row["longitude"])
        if row["latitude"] is not None and row["longitude"] is not None
        else None
    )
    upstream = StreamEndpoints(
        rtsp_url=row["rtsp_url"], hls_url=row["hls_url"], whep_url=row["whep_url"]
    )
    camera_id = str(row["id"])
    return Camera(
        id=camera_id,
        source=row["source"],
        external_id=row["external_id"],
        location=location,
        site_name=row["site_name"],
        district=row["district"],
        department=row["department"],
        owner=row["owner"],
        camera_type=CameraType(row["camera_type"]),
        vendor=row["vendor"],
        vms_platform=row["vms_platform"],
        codec=row["codec"],
        native_width=row["native_width"],
        native_height=row["native_height"],
        endpoints=fanout_endpoints(settings, camera_id, upstream),
        storage_location=row["storage_location"],
        retention_days=row["retention_days"],
        commissioned_at=row["commissioned_at"],
        amc_expires_at=row["amc_expires_at"],
        lifecycle=Lifecycle(row["lifecycle"]),
        catalogue_live=row["catalogue_live"],
        present_in_catalogue=row["present_in_catalogue"],
        last_seen_in_catalogue=row["last_seen_in_catalogue"],
        health=CameraHealth(
            state=HealthState(row["effective_health_state"]),
            reason=row["effective_health_reason"],
            last_heartbeat_at=row["last_heartbeat_at"],
            last_frame_at=row["last_frame_at"],
            observed_fps=row["observed_fps"],
            declared_fps=row["declared_fps"],
            black_frame_ratio=row["black_frame_ratio"],
            tamper_suspected=row["tamper_suspected"],
            consecutive_failures=row["consecutive_failures"],
            loop_epoch=row["loop_epoch"],
            last_error=row["last_error"],
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class CameraRepository:
    def __init__(self, pool: asyncpg.Pool, settings: RegistrySettings) -> None:
        self._pool = pool
        self._s = settings

    # --- reads ---------------------------------------------------------------

    async def get(self, camera_id: str) -> Camera | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM camera_current WHERE id = $1::uuid", camera_id
        )
        return camera_from_row(row, self._s) if row else None

    async def get_by_external(self, source: str, external_id: str) -> Camera | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM camera_current WHERE source = $1 AND external_id = $2",
            source,
            external_id,
        )
        return camera_from_row(row, self._s) if row else None

    async def list(
        self,
        *,
        district: str | None = None,
        department: str | None = None,
        state: HealthState | None = None,
        lifecycle: Lifecycle | None = Lifecycle.ACTIVE,
        bbox: tuple[float, float, float, float] | None = None,
        search: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[Camera]:
        clauses: list[str] = []
        args: list[Any] = []

        def add(clause_template: str, value: Any) -> None:
            args.append(value)
            clauses.append(clause_template.format(n=len(args)))

        if lifecycle is not None:
            add("lifecycle = ${n}", lifecycle.value)
        if district:
            add("district = ${n}", district)
        if department:
            add("department = ${n}", department)
        if state is not None:
            add("effective_health_state = ${n}", state.value)
        if search:
            args.append(f"%{search}%")
            n = len(args)
            clauses.append(
                f"(site_name ILIKE ${n} OR external_id ILIKE ${n} OR district ILIKE ${n})"
            )
        if bbox is not None:
            # min_lon, min_lat, max_lon, max_lat — the MapLibre viewport order.
            # ST_MakeEnvelope takes geometry, so the geography column is cast;
            # the GIST index still serves the predicate.
            args.extend(bbox)
            i = len(args)
            clauses.append(
                f"location IS NOT NULL AND location::geometry && "
                f"ST_MakeEnvelope(${i - 3}, ${i - 2}, ${i - 1}, ${i}, 4326)"
            )

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        args.extend([limit, offset])
        sql = (
            f"SELECT * FROM camera_current {where} "
            f"ORDER BY district NULLS LAST, site_name NULLS LAST, external_id "
            f"LIMIT ${len(args) - 1} OFFSET ${len(args)}"
        )
        rows = await self._pool.fetch(sql, *args)
        return [camera_from_row(r, self._s) for r in rows]

    async def count(self, *, lifecycle: Lifecycle | None = Lifecycle.ACTIVE) -> int:
        if lifecycle is None:
            return await self._pool.fetchval("SELECT count(*) FROM cameras")
        return await self._pool.fetchval(
            "SELECT count(*) FROM cameras WHERE lifecycle = $1", lifecycle.value
        )

    async def health_summary(self) -> dict[str, int]:
        rows = await self._pool.fetch(
            """
            SELECT effective_health_state AS state, count(*) AS n
            FROM camera_current WHERE lifecycle = 'active'
            GROUP BY 1
            """
        )
        summary = {s.value: 0 for s in HealthState}
        for row in rows:
            summary[row["state"]] = row["n"]
        return summary

    # --- writes --------------------------------------------------------------

    async def create(self, payload: CameraCreate) -> Camera:
        row = await self._pool.fetchrow(
            """
            INSERT INTO cameras (
                source, external_id, location, site_name, district, department, owner,
                camera_type, vendor, vms_platform, codec, native_width, native_height,
                declared_fps, rtsp_url, hls_url, whep_url,
                storage_location, retention_days, commissioned_at, amc_expires_at,
                stale_after_s, present_in_catalogue
            ) VALUES (
                $1, $2, $3::geography, $4, $5, $6, $7,
                $8, $9, $10, $11, $12, $13,
                $14, $15, $16, $17,
                $18, $19, $20, $21,
                COALESCE($22, $23), false
            )
            RETURNING id
            """,
            payload.source,
            payload.external_id,
            _point(payload.location),
            payload.site_name,
            payload.district,
            payload.department,
            payload.owner,
            payload.camera_type.value,
            payload.vendor,
            payload.vms_platform,
            payload.codec,
            payload.native_width,
            payload.native_height,
            payload.declared_fps,
            payload.rtsp_url,
            payload.hls_url,
            payload.whep_url,
            payload.storage_location,
            payload.retention_days,
            payload.commissioned_at,
            payload.amc_expires_at,
            payload.stale_after_s,
            self._s.health_stale_after_s,
        )
        created = await self.get(str(row["id"]))
        assert created is not None
        return created

    async def update(self, camera_id: str, payload: CameraUpdate) -> Camera | None:
        fields = payload.model_dump(exclude_unset=True, exclude_none=True)
        if not fields:
            return await self.get(camera_id)

        sets: list[str] = []
        args: list[Any] = []
        for key, value in fields.items():
            if key == "location":
                args.append(_point(payload.location))
                sets.append(f"location = ${len(args)}::geography")
            elif key in {"camera_type", "lifecycle"}:
                args.append(value.value if hasattr(value, "value") else value)
                sets.append(f"{key} = ${len(args)}")
            else:
                args.append(value)
                sets.append(f"{key} = ${len(args)}")
        sets.append("updated_at = now()")
        args.append(camera_id)

        row = await self._pool.fetchrow(
            f"UPDATE cameras SET {', '.join(sets)} WHERE id = ${len(args)}::uuid RETURNING id",
            *args,
        )
        return await self.get(camera_id) if row else None

    async def decommission(self, camera_id: str) -> Camera | None:
        """Retire a camera. Never a DELETE.

        Its detections are evidence, and evidence with a dangling camera
        reference is evidence that cannot be defended in a hearing.
        """
        row = await self._pool.fetchrow(
            """
            UPDATE cameras SET lifecycle = 'decommissioned', updated_at = now()
            WHERE id = $1::uuid RETURNING id
            """,
            camera_id,
        )
        return await self.get(camera_id) if row else None

    async def upsert_from_catalogue(
        self,
        conn: asyncpg.Connection,
        *,
        source: str,
        external_id: str,
        site_name: str | None,
        location: GeoPoint | None,
        codec: str | None,
        native_width: int | None,
        native_height: int | None,
        declared_fps: float | None,
        rtsp_url: str | None,
        hls_url: str | None,
        whep_url: str | None,
        catalogue_live: bool,
        raw: dict,
        seen_at: datetime,
    ) -> tuple[str, bool]:
        """Insert or refresh one catalogue entry. Returns (camera_id, inserted).

        COALESCE on every optional field is the important part: the catalogue is
        authoritative for what it *knows*, not for what it omits. A sync must
        never blank a district an operator typed in because this gateway does not
        carry districts.

        `lifecycle` is deliberately not reset for a decommissioned camera —
        a stale gateway entry must not put a retired camera back in service.
        """
        row = await conn.fetchrow(
            """
            INSERT INTO cameras (
                source, external_id, site_name, location, codec,
                native_width, native_height, declared_fps,
                rtsp_url, hls_url, whep_url,
                catalogue_live, present_in_catalogue, last_seen_in_catalogue, raw
            ) VALUES (
                $1, $2, $3, $4::geography, $5,
                $6, $7, $8,
                $9, $10, $11,
                $12, true, $13, $14::jsonb
            )
            ON CONFLICT (source, external_id) DO UPDATE SET
                site_name              = COALESCE(EXCLUDED.site_name, cameras.site_name),
                location               = COALESCE(EXCLUDED.location, cameras.location),
                codec                  = COALESCE(EXCLUDED.codec, cameras.codec),
                native_width           = COALESCE(EXCLUDED.native_width, cameras.native_width),
                native_height          = COALESCE(EXCLUDED.native_height, cameras.native_height),
                declared_fps           = COALESCE(EXCLUDED.declared_fps, cameras.declared_fps),
                rtsp_url               = COALESCE(EXCLUDED.rtsp_url, cameras.rtsp_url),
                hls_url                = COALESCE(EXCLUDED.hls_url, cameras.hls_url),
                whep_url               = COALESCE(EXCLUDED.whep_url, cameras.whep_url),
                catalogue_live         = EXCLUDED.catalogue_live,
                present_in_catalogue   = true,
                last_seen_in_catalogue = EXCLUDED.last_seen_in_catalogue,
                raw                    = EXCLUDED.raw,
                lifecycle              = CASE
                                             WHEN cameras.lifecycle = 'decommissioned'
                                             THEN 'decommissioned'
                                             ELSE 'active'
                                         END,
                updated_at             = now()
            RETURNING id, (xmax = 0) AS inserted
            """,
            source,
            external_id,
            site_name,
            _point(location),
            codec,
            native_width,
            native_height,
            declared_fps,
            rtsp_url,
            hls_url,
            whep_url,
            catalogue_live,
            seen_at,
            raw,
        )
        return str(row["id"]), row["inserted"]

    async def mark_absent(
        self, conn: asyncpg.Connection, *, source: str, seen_ids: Sequence[str]
    ) -> int:
        """Flag cameras this source no longer lists.

        `absent`, not deleted, and not `unreachable`: a camera dropping out of
        the catalogue is a registry fact, whereas unreachable is a health fact.
        Conflating them would make a gateway re-indexing its estate look like a
        district-wide outage.
        """
        return int(
            await conn.fetchval(
                """
                WITH marked AS (
                    UPDATE cameras
                    SET lifecycle = 'absent', present_in_catalogue = false, updated_at = now()
                    WHERE source = $1 AND lifecycle = 'active'
                      AND NOT (id = ANY($2::uuid[]))
                    RETURNING 1
                )
                SELECT count(*) FROM marked
                """,
                source,
                list(seen_ids),
            )
        )

    # --- health --------------------------------------------------------------

    async def recent_health_history(
        self, camera_id: str, *, window_s: int, limit: int
    ) -> tuple[list[float], list[bool]]:
        """The camera's own recent heartbeats, newest first, for the drift
        baseline and the tamper streak."""
        rows = await self._pool.fetch(
            """
            SELECT measured_fps, tamper_suspected
            FROM camera_heartbeat
            WHERE camera_id = $1::uuid
              AND observed_at > now() - make_interval(secs => $2)
            ORDER BY observed_at DESC
            LIMIT $3
            """,
            camera_id,
            window_s,
            limit,
        )
        fps = [r["measured_fps"] for r in rows if r["measured_fps"] is not None]
        tamper = [r["tamper_suspected"] for r in rows]
        return fps, tamper

    async def prune_heartbeats(self, *, retention_days: int) -> int:
        """Delete heartbeats older than the retention window; returns the count.

        A no-op where TimescaleDB is present, because its retention policy has
        already dropped the chunks. It exists for the deployments that do not
        have the extension, where the alternative is a table that only ever
        grows.

        Deliberately not batched: at one row per camera per 10 s, an hourly pass
        deletes a bounded slice, and a `DELETE` over an indexed timestamp on a
        few hundred thousand rows is cheaper than the machinery to chunk it.
        """
        status = await self._pool.execute(
            """
            DELETE FROM camera_heartbeat
            WHERE observed_at < now() - make_interval(days => $1)
            """,
            retention_days,
        )
        # asyncpg returns the raw command tag, e.g. "DELETE 1204".
        return int(status.rsplit(" ", 1)[-1]) if status.startswith("DELETE") else 0

    async def record_heartbeat(
        self, camera_id: str, heartbeat: Heartbeat, verdict: HealthVerdict
    ) -> None:
        observed_at = heartbeat.observed_at or datetime.now(UTC)
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO camera_heartbeat (
                    camera_id, observed_at, worker_id, connected, measured_fps,
                    last_frame_at, frames_decoded, consecutive_failures,
                    black_frame_ratio, tamper_suspected, loop_epoch, last_error
                ) VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                """,
                camera_id,
                observed_at,
                heartbeat.worker_id,
                heartbeat.connected,
                heartbeat.measured_fps,
                heartbeat.last_frame_at,
                heartbeat.frames_decoded,
                heartbeat.consecutive_failures,
                heartbeat.black_frame_ratio,
                heartbeat.tamper_suspected,
                heartbeat.loop_epoch,
                heartbeat.last_error,
            )
            # The denormalised cache on `cameras`. `last_heartbeat_at` uses
            # GREATEST so an out-of-order report from a slow worker cannot wind
            # the clock back and make a live camera look stale.
            await conn.execute(
                """
                UPDATE cameras SET
                    health_state         = $2,
                    health_reason        = $3,
                    last_heartbeat_at    = GREATEST(COALESCE(last_heartbeat_at, $4), $4),
                    last_frame_at        = COALESCE($5, last_frame_at),
                    observed_fps         = COALESCE($6, observed_fps),
                    black_frame_ratio    = COALESCE($7, black_frame_ratio),
                    tamper_suspected     = $8,
                    consecutive_failures = $9,
                    loop_epoch           = $10,
                    last_error           = $11,
                    updated_at           = now()
                WHERE id = $1::uuid
                """,
                camera_id,
                verdict.state.value,
                verdict.reason,
                observed_at,
                heartbeat.last_frame_at,
                heartbeat.measured_fps,
                heartbeat.black_frame_ratio,
                heartbeat.tamper_suspected,
                heartbeat.consecutive_failures,
                heartbeat.loop_epoch,
                heartbeat.last_error,
            )

    # --- sync bookkeeping ----------------------------------------------------

    async def start_sync_run(self, source: str) -> int:
        return await self._pool.fetchval(
            "INSERT INTO catalogue_sync_run (source) VALUES ($1) RETURNING id", source
        )

    async def finish_sync_run(self, run_id: int, result: SyncResult) -> None:
        await self._pool.execute(
            """
            UPDATE catalogue_sync_run SET
                finished_at = now(), ok = $2,
                cameras_seen = $3, cameras_added = $4,
                cameras_updated = $5, cameras_absent = $6,
                codec_mix = $7::jsonb, error = $8
            WHERE id = $1
            """,
            run_id,
            result.ok,
            result.cameras_seen,
            result.cameras_added,
            result.cameras_updated,
            result.cameras_absent,
            result.codec_mix,
            result.error,
        )

    async def last_sync_runs(self, limit: int = 10) -> list[SyncResult]:
        rows = await self._pool.fetch(
            "SELECT * FROM catalogue_sync_run ORDER BY started_at DESC LIMIT $1", limit
        )
        return [
            SyncResult(
                source=r["source"],
                ok=bool(r["ok"]),
                started_at=r["started_at"],
                finished_at=r["finished_at"],
                cameras_seen=r["cameras_seen"],
                cameras_added=r["cameras_added"],
                cameras_updated=r["cameras_updated"],
                cameras_absent=r["cameras_absent"],
                codec_mix=r["codec_mix"] or {},
                error=r["error"],
            )
            for r in rows
        ]

    # --- fan-out -------------------------------------------------------------

    async def desired_mediamtx_paths(self) -> dict[str, str]:
        """Path name → upstream URL, for every camera worth fanning out.

        Cameras the catalogue reports as not live are excluded: §5 says to
        confirm live status in `/api/ingest` before reporting a camera down, and
        the corollary is that configuring a pull against a known-dead feed only
        buys reconnect noise.
        """
        rows = await self._pool.fetch(
            """
            SELECT id, rtsp_url FROM cameras
            WHERE lifecycle = 'active' AND catalogue_live AND rtsp_url IS NOT NULL
            """
        )
        return {f"cam-{row['id']}": row["rtsp_url"] for row in rows}
