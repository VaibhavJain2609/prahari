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
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg

from .config import RegistrySettings
from .crypto import decrypt_credential, encrypt_credential
from .health import HealthVerdict
from .mediamtx import fanout_endpoints
from .models import (
    Camera,
    CameraCreate,
    CameraHealth,
    CameraPreview,
    CameraType,
    CameraUpdate,
    GeoPoint,
    HealthState,
    Heartbeat,
    HeartbeatSample,
    Lifecycle,
    Org,
    OrgCreate,
    OrgKind,
    StreamEndpoints,
    SyncResult,
    WorkerAssignment,
    WorkerRegistration,
)

log = logging.getLogger(__name__)

_MAX_OBSERVED_AT_SKEW_S = 60.0
"""How far into the future a heartbeat's `observed_at` may run ahead of the
registry's clock before we stop trusting it. `last_heartbeat_at` is updated
with GREATEST(), so a worker with a wildly wrong clock (or a malicious one)
could stamp next year and permanently suppress the staleness overlay for that
camera — the camera would read healthy forever after dying. Small skews are
real (NTP is not perfect), so we clamp rather than reject."""


def _clamp_observed_at(observed_at: datetime | None) -> datetime:
    """The `observed_at` a heartbeat is actually recorded under.

    `None` becomes now — the worker did not timestamp it. A timestamp more than
    `_MAX_OBSERVED_AT_SKEW_S` ahead of now is clamped to now rather than
    rejected, because a worker with a fast clock is still reporting a live
    camera and a 422 would just make it retry forever; but its timestamp must
    not be allowed to poison `last_heartbeat_at` via GREATEST.
    """
    now = datetime.now(UTC)
    if observed_at is None:
        return now
    # A naive timestamp is interpreted as UTC — the workers all run UTC, and
    # comparing naive against aware would raise here instead.
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    if (observed_at - now).total_seconds() > _MAX_OBSERVED_AT_SKEW_S:
        log.warning(
            "heartbeat observed_at %s is >%.0fs in the future; clamped to now",
            observed_at.isoformat(),
            _MAX_OBSERVED_AT_SKEW_S,
        )
        return now
    return observed_at


def _point(location: GeoPoint | None) -> str | None:
    """WKT for a geography(Point, 4326). Longitude first — the commonest way to
    silently put every camera in the wrong hemisphere."""
    if location is None:
        return None
    return f"SRID=4326;POINT({location.longitude} {location.latitude})"


def redact_url_credentials(url: str) -> str:
    """The same URL minus its userinfo — `rtsp://user:pass@host:554/x` becomes
    `rtsp://host:554/x`.

    Exists for the diagnostic surface (`GET /api/v1/streams/paths`): the
    mapping `desired_mediamtx_paths` returns embeds decrypted DVR credentials
    because MediaMTX's reconcile needs them to pull the source. That secret
    must never leave the process in an HTTP response, so the endpoint renders
    every URL through this. The reconcile path keeps the credentialed form —
    stripping there would silently break every authenticated pull.

    The query string is dropped entirely: some DVR/NVR lines accept
    `?username=&password=` auth, and a scheme+host+path answer is all the
    diagnostic surface needs. `parts.port` is guarded — a malformed port in a
    stored URL must degrade to the unredacted port's absence, not a 500.
    """
    parts = urlsplit(url)
    netloc = parts.hostname or ""
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"  # IPv6 literal — hostname strips the brackets
    try:
        if parts.port is not None:
            netloc += f":{parts.port}"
    except ValueError:
        pass  # malformed port — emit host-only rather than fail the endpoint
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _with_credentials(url: str, username: str | None, password: str) -> str:
    """Inject decrypted userinfo into an RTSP URL for the MediaMTX source
    config — `rtsp://host:port/path` becomes `rtsp://user:pass@host:port/path`.
    Any userinfo already on the URL is replaced, not merged, so a stored
    credential is always the one actually used."""
    parts = urlsplit(url)
    # A password may itself contain `@` or `:` (an operator does not choose
    # a DVR's factory-set credential); left unescaped, either character
    # breaks netloc parsing and MediaMTX silently connects to the wrong
    # host or fails auth. `safe=""` quotes both.
    userinfo = f"{quote(username or '', safe='')}:{quote(password, safe='')}"
    netloc = f"{userinfo}@{parts.hostname or ''}"
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def camera_from_row(row: asyncpg.Record, settings: RegistrySettings) -> Camera:
    """Map a `camera_current` row onto the API model.

    Reads `effective_health_state`, not `health_state`: the view has already
    applied the staleness overlay, and the raw column is the last verdict a
    heartbeat produced, which for a camera that went dark an hour ago still says
    "healthy".

    The row's `rtsp_url`/`hls_url`/`whep_url` columns never leave this
    function: they are the upstream (gateway/DVR) pull URLs, and the only
    code allowed to read them for connection purposes is
    `desired_mediamtx_paths`. What a response gets instead is the fan-out
    half — populated only when a pullable upstream exists, mirroring the
    predicate `desired_mediamtx_paths` reconciles on, so `endpoints` and
    `preview.available` can never advertise a path MediaMTX would not have.
    """
    location = (
        GeoPoint(latitude=row["latitude"], longitude=row["longitude"])
        if row["latitude"] is not None and row["longitude"] is not None
        else None
    )
    streamable = row["rtsp_url"] is not None
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
        org_id=str(row["org_id"]) if row["org_id"] is not None else None,
        adapter=row["adapter"],
        camera_type=CameraType(row["camera_type"]),
        vendor=row["vendor"],
        vms_platform=row["vms_platform"],
        codec=row["codec"],
        native_width=row["native_width"],
        native_height=row["native_height"],
        endpoints=fanout_endpoints(settings, camera_id) if streamable else StreamEndpoints(),
        preview=CameraPreview(available=streamable),
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
    #
    # `scope` is a required, non-defaulted keyword on every read below — an
    # ltree path (see orgs.path in migrations/005_orgs.sql). It is never
    # optional the way `district`/`department` are: a caller that forgets to
    # narrow the *board* still narrows the *scope*, because the parameter
    # cannot be omitted and still type-check. The predicate itself is one
    # join: `o.path <@ $scope::ltree` reads camera + everything under it.
    #
    # This is deliberately not Postgres row-level security — see
    # docs/ORG-TIERS-DESIGN.md §7. Every caller of this repository is trusted
    # to pass the *correct* scope; what this buys is that it cannot pass none.

    async def get(self, camera_id: str, *, scope: str) -> Camera | None:
        row = await self._pool.fetchrow(
            """
            SELECT cc.* FROM camera_current cc
            JOIN orgs o ON o.id = cc.org_id
            WHERE cc.id = $1::uuid AND o.path <@ $2::ltree
            """,
            camera_id,
            scope,
        )
        return camera_from_row(row, self._s) if row else None

    async def get_by_external(self, source: str, external_id: str, *, scope: str) -> Camera | None:
        row = await self._pool.fetchrow(
            """
            SELECT cc.* FROM camera_current cc
            JOIN orgs o ON o.id = cc.org_id
            WHERE cc.source = $1 AND cc.external_id = $2 AND o.path <@ $3::ltree
            """,
            source,
            external_id,
            scope,
        )
        return camera_from_row(row, self._s) if row else None

    async def list(
        self,
        *,
        scope: str,
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
        args: list[Any] = [scope]
        clauses.append("o.path <@ $1::ltree")

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
            f"SELECT cc.* FROM camera_current cc JOIN orgs o ON o.id = cc.org_id {where} "
            f"ORDER BY district NULLS LAST, site_name NULLS LAST, external_id "
            f"LIMIT ${len(args) - 1} OFFSET ${len(args)}"
        )
        rows = await self._pool.fetch(sql, *args)
        return [camera_from_row(r, self._s) for r in rows]

    async def count(self, *, scope: str, lifecycle: Lifecycle | None = Lifecycle.ACTIVE) -> int:
        if lifecycle is None:
            return await self._pool.fetchval(
                """
                SELECT count(*) FROM cameras c JOIN orgs o ON o.id = c.org_id
                WHERE o.path <@ $1::ltree
                """,
                scope,
            )
        return await self._pool.fetchval(
            """
            SELECT count(*) FROM cameras c JOIN orgs o ON o.id = c.org_id
            WHERE o.path <@ $1::ltree AND c.lifecycle = $2
            """,
            scope,
            lifecycle.value,
        )

    async def health_summary(self, *, scope: str) -> dict[str, int]:
        rows = await self._pool.fetch(
            """
            SELECT cc.effective_health_state AS state, count(*) AS n
            FROM camera_current cc JOIN orgs o ON o.id = cc.org_id
            WHERE cc.lifecycle = 'active' AND o.path <@ $1::ltree
            GROUP BY 1
            """,
            scope,
        )
        summary = {s.value: 0 for s in HealthState}
        for row in rows:
            summary[row["state"]] = row["n"]
        return summary

    # --- org resolution --------------------------------------------------------

    async def org_id_for_path(self, path: str) -> str | None:
        row = await self._pool.fetchval("SELECT id FROM orgs WHERE path = $1::ltree", path)
        return str(row) if row is not None else None

    async def org_path_for_id(self, org_id: str) -> str | None:
        row = await self._pool.fetchval("SELECT path::text FROM orgs WHERE id = $1::uuid", org_id)
        return row

    async def _resolve_org(self, org_id: str | None) -> tuple[str, str]:
        """(org_id, org_path) for a create — the payload's own org_id if it
        named one, otherwise `sync_default_org_path`. Raises if neither
        resolves, rather than silently falling through to no org: a camera
        that fails to get an org must fail to be created, not become
        invisible to every scoped read the moment it lands."""
        if org_id is not None:
            path = await self.org_path_for_id(org_id)
            if path is None:
                raise ValueError(f"no org {org_id!r}")
            return org_id, path
        path = self._s.sync_default_org_path
        resolved_id = await self.org_id_for_path(path)
        if resolved_id is None:
            raise ValueError(
                f"sync_default_org_path {path!r} does not exist — "
                "was migration 005's seed row removed?"
            )
        return resolved_id, path

    # --- writes --------------------------------------------------------------

    async def create(self, payload: CameraCreate) -> Camera:
        org_id, org_path = await self._resolve_org(payload.org_id)
        stream_secret = (
            encrypt_credential(payload.stream_password, self._s.credential_key)
            if payload.stream_password is not None
            else None
        )
        row = await self._pool.fetchrow(
            """
            INSERT INTO cameras (
                source, external_id, location, site_name, district, department, owner,
                org_id, adapter, stream_username, stream_secret,
                camera_type, vendor, vms_platform, codec, native_width, native_height,
                declared_fps, rtsp_url, hls_url, whep_url,
                storage_location, retention_days, commissioned_at, amc_expires_at,
                stale_after_s, present_in_catalogue
            ) VALUES (
                $1, $2, $3::geography, $4, $5, $6, $7,
                $8::uuid, $9, $10, $11,
                $12, $13, $14, $15, $16, $17,
                $18, $19, $20, $21,
                $22, $23, $24, $25,
                COALESCE($26::integer, $27::integer), false
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
            org_id,
            payload.adapter,
            payload.stream_username,
            stream_secret,
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
        created = await self.get(str(row["id"]), scope=org_path)
        assert created is not None
        return created

    async def update(self, camera_id: str, payload: CameraUpdate, *, scope: str) -> Camera | None:
        """`scope` gates which camera the caller may touch, exactly as it
        gates reads — a WHERE clause, not a client-side check. Reassigning
        `org_id` outside the caller's own scope is allowed (it is how a camera
        moves between orgs) but the post-update read below then uses the
        *original* scope, so a reassignment that moves the camera out of the
        caller's own subtree correctly reports back as no-longer-visible."""
        fields = payload.model_dump(exclude_unset=True, exclude_none=True)
        if not fields:
            return await self.get(camera_id, scope=scope)

        sets: list[str] = []
        args: list[Any] = []
        for key, value in fields.items():
            if key == "location":
                args.append(_point(payload.location))
                sets.append(f"location = ${len(args)}::geography")
            elif key in {"camera_type", "lifecycle"}:
                args.append(value.value if hasattr(value, "value") else value)
                sets.append(f"{key} = ${len(args)}")
            elif key == "stream_password":
                # No 1:1 column: the plaintext field name intentionally
                # differs from `stream_secret` so nothing outside this
                # branch can accidentally write a credential unencrypted.
                args.append(encrypt_credential(value, self._s.credential_key))
                sets.append(f"stream_secret = ${len(args)}")
            else:
                args.append(value)
                sets.append(f"{key} = ${len(args)}")
        sets.append("updated_at = now()")
        args.append(camera_id)
        args.append(scope)

        row = await self._pool.fetchrow(
            f"""
            UPDATE cameras SET {", ".join(sets)}
            WHERE id = ${len(args) - 1}::uuid
              AND org_id IN (SELECT id FROM orgs WHERE path <@ ${len(args)}::ltree)
            RETURNING id
            """,
            *args,
        )
        return await self.get(camera_id, scope=scope) if row else None

    async def decommission(self, camera_id: str, *, scope: str) -> Camera | None:
        """Retire a camera. Never a DELETE.

        Its detections are evidence, and evidence with a dangling camera
        reference is evidence that cannot be defended in a hearing.
        """
        row = await self._pool.fetchrow(
            """
            UPDATE cameras SET lifecycle = 'decommissioned', updated_at = now()
            WHERE id = $1::uuid
              AND org_id IN (SELECT id FROM orgs WHERE path <@ $2::ltree)
            RETURNING id
            """,
            camera_id,
            scope,
        )
        return await self.get(camera_id, scope=scope) if row else None

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
        default_org_id: str,
    ) -> tuple[str, bool]:
        """Insert or refresh one catalogue entry. Returns (camera_id, inserted).

        COALESCE on every optional field is the important part: the catalogue is
        authoritative for what it *knows*, not for what it omits. A sync must
        never blank a district an operator typed in because this gateway does not
        carry districts.

        `lifecycle` is deliberately not reset for a decommissioned camera —
        a stale gateway entry must not put a retired camera back in service.

        `default_org_id` is written on INSERT only, exactly like district and
        department already are — once a local body reassigns a synced camera
        to its own org, no future sync moves it back. `org_id` is therefore
        absent from the ON CONFLICT SET list on purpose, not by oversight.
        """
        row = await conn.fetchrow(
            """
            INSERT INTO cameras (
                source, external_id, site_name, location, codec,
                native_width, native_height, declared_fps,
                rtsp_url, hls_url, whep_url,
                catalogue_live, present_in_catalogue, last_seen_in_catalogue, raw,
                org_id
            ) VALUES (
                $1, $2, $3, $4::geography, $5,
                $6, $7, $8,
                $9, $10, $11,
                $12, true, $13, $14::jsonb,
                $15::uuid
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
            default_org_id,
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

    async def health_history(
        self,
        camera_id: str,
        *,
        scope: str,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[HeartbeatSample]:
        """Stored heartbeats for one camera, newest first.

        Backs `GET /cameras/{id}/health-history` — the console's camera detail
        drawer. Scope-gated by the same `o.path <@ scope` predicate as `get`:
        a heartbeat is camera data, and camera data is only visible inside the
        caller's subtree. `since` bounds the window; `limit` is bounded by the
        endpoint (≤500) so a drawer cannot pull the whole retention window.
        """
        rows = await self._pool.fetch(
            """
            SELECT h.observed_at, h.worker_id, h.connected, h.measured_fps,
                   h.last_frame_at, h.frames_decoded, h.consecutive_failures,
                   h.black_frame_ratio, h.tamper_suspected, h.loop_epoch, h.last_error
            FROM camera_heartbeat h
            JOIN cameras c ON c.id = h.camera_id
            JOIN orgs o ON o.id = c.org_id
            WHERE h.camera_id = $1::uuid
              AND o.path <@ $2::ltree
              AND ($3::timestamptz IS NULL OR h.observed_at >= $3)
            ORDER BY h.observed_at DESC
            LIMIT $4
            """,
            camera_id,
            scope,
            since,
            limit,
        )
        return [
            HeartbeatSample(
                observed_at=r["observed_at"],
                worker_id=r["worker_id"],
                connected=r["connected"],
                measured_fps=r["measured_fps"],
                last_frame_at=r["last_frame_at"],
                frames_decoded=r["frames_decoded"],
                consecutive_failures=r["consecutive_failures"],
                black_frame_ratio=r["black_frame_ratio"],
                tamper_suspected=r["tamper_suspected"],
                loop_epoch=r["loop_epoch"],
                last_error=r["last_error"],
            )
            for r in rows
        ]

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
        observed_at = _clamp_observed_at(heartbeat.observed_at)
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

        A locally-registered camera's credential is decrypted here and only
        here: this is the one place the raw upstream URL is assembled, and the
        result embeds `user:pass` in each URL.

        **Callers must not hand this mapping to an HTTP response.** It exists
        for the MediaMTX API only — `MediaMTXClient.reconcile` needs the
        credentialed URL to configure the source pull. The one HTTP consumer,
        `GET /api/v1/streams/paths`, renders every value through
        `redact_url_credentials` before serialising; `fanout_endpoints()` in
        `mediamtx.py` hands callers only the MediaMTX-fronted public URL.
        """
        rows = await self._pool.fetch(
            """
            SELECT id, rtsp_url, stream_username, stream_secret FROM cameras
            WHERE lifecycle = 'active' AND catalogue_live AND rtsp_url IS NOT NULL
            """
        )
        paths: dict[str, str] = {}
        for row in rows:
            url = row["rtsp_url"]
            if row["stream_secret"] is not None:
                password = decrypt_credential(bytes(row["stream_secret"]), self._s.credential_key)
                url = _with_credentials(url, row["stream_username"], password)
            paths[f"cam-{row['id']}"] = url
        return paths


def shard_membership(worker_id: str, alive_worker_ids: Sequence[str]) -> tuple[int, int]:
    """A worker's modulo-shard coordinates inside the alive fleet.

    `(index, count)`: index is the worker's position in the sorted alive set,
    count is the set's size. Sorting `worker_id` is what makes the coordinates
    deterministic — every caller and every worker computes the same partition
    of the same ordered list, with no leader election and no rendezvous.
    Raises `ValueError` for a worker that is not in the alive set, which is
    unreachable in `WorkerRepository.register` (the upsert refreshes
    `last_seen` first) but must fail loudly rather than silently assign index
    -1 to a caller that skipped registering.
    """
    ordered = sorted(alive_worker_ids)
    return ordered.index(worker_id), len(ordered)


class WorkerRepository:
    """The `workers` table: ingest-fleet membership and camera sharding.

    Membership is a lease, not a lock. `register` upserts `last_seen`; the
    alive set is everyone within `2 * assignment_lease_s` of now; and
    `prune_stale` deletes rows dead for `3 *` the lease. There is no DELETE —
    the only teardown a killed pod can be relied on to perform is expiring.

    Sharding is modulo, not consistent-hashing: `shard_count` changes on every
    membership change anyway (KEDA pods in and out), so hash-slot stability
    would buy nothing. `(row_number - 1) % shard_count` over `ORDER BY id` is
    deterministic, a one-line proof of full coverage, and rebalances the whole
    estate on each scale event — a camera briefly claimed by two workers
    mid-reshard is tolerated: both read the same MediaMTX fan-out path, and a
    path holds exactly one upstream pull no matter how many readers attach.
    Only a camera with no fan-out URL (the direct-gateway fallback) can be
    double-pulled upstream, and only for the seconds the two owners overlap.
    """

    # Membership horizons, in multiples of assignment_lease_s. Two misses give
    # a slow-but-alive worker slack; the row itself lingers one more lease so
    # a worker can be re-registered rather than re-created from nothing.
    _ALIVE_LEASES = 2
    _REAP_LEASES = 3

    def __init__(self, pool: asyncpg.Pool, settings: RegistrySettings) -> None:
        self._pool = pool
        self._s = settings

    async def register(self, worker_id: str) -> WorkerRegistration:
        """Upsert the worker's lease and compute its current shard coordinates.

        One round trip of writes plus the membership read, all inside a
        transaction so a concurrent register cannot observe the table between
        the upsert and the count. The computed coordinates are persisted back
        onto the row — `workers.shard_index`/`shard_count` then answer "what
        was this pod last told" for debugging without replaying the query.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO workers (worker_id, registered_at, last_seen)
                VALUES ($1, now(), now())
                ON CONFLICT (worker_id) DO UPDATE SET last_seen = now()
                """,
                worker_id,
            )
            alive = await self.alive_worker_ids(conn)
            index, count = shard_membership(worker_id, alive)
            await conn.execute(
                "UPDATE workers SET shard_index = $2, shard_count = $3 WHERE worker_id = $1",
                worker_id,
                index,
                count,
            )
        return WorkerRegistration(
            worker_id=worker_id,
            shard_index=index,
            shard_count=count,
            lease_s=self._s.assignment_lease_s,
        )

    async def alive_worker_ids(self, conn: asyncpg.Connection | None = None) -> list[str]:
        """Worker ids whose lease is still warm, in shard order.

        Ordered by `worker_id` — not `registered_at`, which would make a
        pod's shard identity depend on when it happened to boot — so the
        ordering is a pure function of *who* is alive, and the same set yields
        the same partition for every caller.
        """
        rows = await (conn or self._pool).fetch(
            """
            SELECT worker_id FROM workers
            WHERE last_seen >= now() - make_interval(secs => $1)
            ORDER BY worker_id
            """,
            self._s.assignment_lease_s * self._ALIVE_LEASES,
        )
        return [r["worker_id"] for r in rows]

    async def assignment(self, worker_id: str, *, scope: str) -> WorkerAssignment | None:
        """The camera slice for an already-registered, still-alive worker.

        Returns None when `worker_id` is not in the alive set — the endpoint
        turns that into a 404. Register-or-refresh used to happen inside this
        call, which made identity caller-asserted: anyone holding the internal
        token could mint a phantom worker (and read its would-be slice, which
        carries the credential-bearing fan-out URLs) just by querying with a
        made-up id. Membership is now granted ONLY by `register` — a worker
        calls register() then polls this, which is also its lease keep-alive,
        so the alive check doubles as the liveness gate: a worker whose lease
        has expired must re-announce itself rather than keep pulling
        assignments forever."""
        alive = await self.alive_worker_ids()
        if worker_id not in alive:
            return None
        shard_index, shard_count = shard_membership(worker_id, alive)
        cameras = await self.shard_of_cameras(
            scope=scope,
            shard_index=shard_index,
            shard_count=shard_count,
        )
        return WorkerAssignment(
            worker_id=worker_id,
            shard_index=shard_index,
            shard_count=shard_count,
            lease_s=self._s.assignment_lease_s,
            cameras=cameras,
        )

    async def shard_of_cameras(
        self, *, scope: str, shard_index: int, shard_count: int
    ) -> list[Camera]:
        """The `shard_index`-of-`shard_count` slice of the active estate.

        `row_number() OVER (ORDER BY id)` numbers the active, in-scope cameras
        against a key that never changes (the registry's own uuid — external
        ids rotate, `site_name`/`district` are mutable and nullable). Slice
        membership is `(rn - 1) % shard_count = shard_index`: consecutive rows
        land on consecutive workers, so the partition is as even as integer
        division allows and is recomputed from scratch on every call — no
        stored assignment to drift from the catalogue between refreshes.
        """
        rows = await self._pool.fetch(
            """
            SELECT * FROM (
                SELECT cc.*, row_number() OVER (ORDER BY cc.id) AS shard_rn
                FROM camera_current cc
                JOIN orgs o ON o.id = cc.org_id
                WHERE cc.lifecycle = 'active' AND o.path <@ $1::ltree
            ) numbered
            WHERE (numbered.shard_rn - 1) % $2 = $3
            ORDER BY numbered.shard_rn
            """,
            scope,
            shard_count,
            shard_index,
        )
        return [camera_from_row(r, self._s) for r in rows]

    async def prune_stale(self) -> int:
        """Delete workers whose lease expired `_REAP_LEASES`x ago; returns count.

        Hygiene only — membership math already ignores anything past
        `_ALIVE_LEASES`x the lease, so a slow prune can never strand a camera
        on a dead pod. It exists so `workers` does not accumulate one
        permanent row per pod a deployment has ever run.
        """
        status = await self._pool.execute(
            """
            DELETE FROM workers
            WHERE last_seen < now() - make_interval(secs => $1)
            """,
            self._s.assignment_lease_s * self._REAP_LEASES,
        )
        return int(status.rsplit(" ", 1)[-1]) if status.startswith("DELETE") else 0


def _org_from_row(row: asyncpg.Record) -> Org:
    return Org(
        id=str(row["id"]),
        parent_id=str(row["parent_id"]) if row["parent_id"] is not None else None,
        path=row["path"],
        kind=OrgKind(row["kind"]),
        name=row["name"],
        created_at=row["created_at"],
    )


class OrgRepository:
    """The org tree itself — separate from `CameraRepository` because it has
    its own identity (an org is not a camera attribute, cameras merely
    reference one) and its own callers: the admin surface Stage 2 adds to the
    BFF, plus the seed step any gate test needs to set up a scope to test
    against.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create(self, payload: OrgCreate) -> Org:
        if payload.parent_id is None:
            path = payload.label
        else:
            parent = await self.get(payload.parent_id)
            if parent is None:
                raise ValueError(f"no org {payload.parent_id!r}")
            path = f"{parent.path}.{payload.label}"
        row = await self._pool.fetchrow(
            """
            INSERT INTO orgs (parent_id, path, kind, name)
            VALUES ($1::uuid, $2::ltree, $3, $4)
            RETURNING id, parent_id, path::text AS path, kind, name, created_at
            """,
            payload.parent_id,
            path,
            payload.kind.value,
            payload.name,
        )
        return _org_from_row(row)

    async def get(self, org_id: str) -> Org | None:
        row = await self._pool.fetchrow(
            "SELECT id, parent_id, path::text AS path, kind, name, created_at "
            "FROM orgs WHERE id = $1::uuid",
            org_id,
        )
        return _org_from_row(row) if row else None

    async def get_by_path(self, path: str) -> Org | None:
        row = await self._pool.fetchrow(
            "SELECT id, parent_id, path::text AS path, kind, name, created_at "
            "FROM orgs WHERE path = $1::ltree",
            path,
        )
        return _org_from_row(row) if row else None

    async def list_subtree(self, scope: str) -> list[Org]:
        """Every org at or below `scope` — what a console's org-admin screen
        renders for a principal, and what Stage 2's user-management endpoints
        validate a target `org_id` against before granting a role there."""
        rows = await self._pool.fetch(
            "SELECT id, parent_id, path::text AS path, kind, name, created_at "
            "FROM orgs WHERE path <@ $1::ltree ORDER BY path",
            scope,
        )
        return [_org_from_row(r) for r in rows]
