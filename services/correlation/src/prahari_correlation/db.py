"""Postgres persistence for sightings — the durable half of the store.

Same shape as `prahari_registry.db`: the service applies its own schema on
startup (a separate migration Job would add an ordering constraint to
`helm install`; in-process means the service is either up with a correct
schema or not up at all). The migration runner is deliberately a copy of the
registry's, not a shared import — correlation must not grow a dependency on
the registry package, and the runner is 60 lines.

`PostgresSightings` implements the `SightingSource` protocol from `store.py`,
so `routes.build_route` queries it exactly the way it queries the in-memory
`DetectionStore`. Rows are written by the detection consumer BEFORE the
stream entry is XACKed (consumer.py): a sighting is durable before it is
acknowledged, and a crash between persist and ack just redelivers into an
`ON CONFLICT DO NOTHING` — at-least-once, deduplicated by `detection_id`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg
from prahari.v1 import events_pb2

from .store import _dedup_key, plate_key, wall_clock_s

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "MIGRATIONS_DIR",
    "PostgresSightings",
    "apply_migrations",
    "create_pool",
]

log = logging.getLogger(__name__)

# Same advisory-lock constant as the registry's runner — correlation shares
# the database, and sharing the lock serialises both services' startup
# migrations against each other. The ledger table (`schema_migration`) is
# shared too; version strings come from filenames and cannot collide
# (`001_correlation_sightings` vs registry's `001_extensions` etc.).
_MIGRATION_LOCK_ID = 0x5052_4148_4152_49  # "PRAHARI"


def _resolve_migrations_dir() -> Path:
    """Where the .sql files live — same dual-path trick as the registry:
    inside a built wheel they sit next to the package (the force-include in
    pyproject.toml), in an editable checkout they are two levels up at the
    service root. Checking both means the same code path runs in the
    container and on the laptop."""
    packaged = Path(__file__).parent / "migrations"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "migrations"


MIGRATIONS_DIR = _resolve_migrations_dir()


async def _init_connection(conn: asyncpg.Connection) -> None:
    """asyncpg returns jsonb as a string unless told otherwise."""
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(database_url: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        database_url,
        # One hot replica of a laptop-scale service does not need registry-sized
        # pools: reads are per-route-query, writes are the single consumer task.
        min_size=1,
        max_size=5,
        init=_init_connection,
        # A pod that cannot reach Postgres should fail its readiness probe and
        # be restarted, not hang holding a connection attempt open.
        command_timeout=30.0,
    )


def _migration_files(directory: Path | None = None) -> list[Path]:
    d = directory or MIGRATIONS_DIR
    if not d.is_dir():
        raise FileNotFoundError(f"migrations directory not found: {d}")
    return sorted(d.glob("*.sql"))


async def apply_migrations(pool: asyncpg.Pool, directory: Path | None = None) -> list[str]:
    """Apply every unapplied migration, in filename order. Returns what ran.

    Identical semantics to the registry's runner: applied migrations are
    checksummed, and editing a file that has already run is rejected rather
    than ignored — that failure is what leaves the laptop and the cloud on
    different schemas while both report success.
    """
    applied: list[str] = []

    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migration (
                version    text PRIMARY KEY,
                checksum   text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_ID)
        try:
            for path in _migration_files(directory):
                version = path.stem
                sql = path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()

                existing = await conn.fetchval(
                    "SELECT checksum FROM schema_migration WHERE version = $1", version
                )
                if existing is not None:
                    if existing != checksum:
                        raise RuntimeError(
                            f"migration {version} has changed since it was applied "
                            f"(recorded {existing[:12]}, file {checksum[:12]}). "
                            "Add a new migration instead of editing an applied one."
                        )
                    continue

                log.info("applying migration %s", version)
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migration (version, checksum) VALUES ($1, $2)",
                        version,
                        checksum,
                    )
                applied.append(version)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_ID)

    return applied


def _ts(epoch_s: float) -> datetime:
    return datetime.fromtimestamp(epoch_s, tz=UTC)


def _row_to_detection(row) -> events_pb2.VehicleDetection:  # noqa: ANN001 -- asyncpg.Record
    """Rebuild the exact detection off the stored blob. `detection_pb` is the
    record (module docstring); asyncpg hands bytea back as memoryview."""
    return events_pb2.VehicleDetection.FromString(bytes(row["detection_pb"]))


class PostgresSightings:
    """`SightingSource` over `correlation_sightings`, plus the write path the
    consumer calls. All methods run on the event loop that created the pool —
    in this service that is the FastAPI loop, which the consumer task also
    runs on, so no cross-loop plumbing is needed."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def insert_detection(
        self, detection: events_pb2.VehicleDetection, stream_entry_id: str
    ) -> None:
        """Insert one sighting, idempotently. Called for every detection the
        store accepted — and for duplicates too, because a redelivered entry
        whose first persist failed arrives as an in-memory duplicate and must
        still be retried; `ON CONFLICT DO NOTHING` makes the retry free."""
        plated = detection.HasField("plate") and bool(detection.plate.normalised_text)
        skeleton_key = plate_key(detection.plate.normalised_text) if plated else ""
        char_confidences = list(detection.plate.char_confidence) if plated else None
        await self._pool.execute(
            """
            INSERT INTO correlation_sightings
                (detection_id, camera_id, plate_skeleton, plate_normalised,
                 char_confidences, observed_at, stream_entry_id, detection_pb)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (detection_id) DO NOTHING
            """,
            detection.detection_id or _dedup_key(detection),
            detection.camera_id,
            skeleton_key or None,
            detection.plate.normalised_text if plated else None,
            char_confidences or None,
            _ts(wall_clock_s(detection)),
            stream_entry_id,
            detection.SerializeToString(),
        )

    async def sightings_for_plate(
        self,
        raw_plate_text: str,
        *,
        since_s: float | None = None,
        limit: int | None = None,
    ) -> list[events_pb2.VehicleDetection]:
        """Every persisted sighting of the plate's skeleton key, oldest first —
        the same contract as `DetectionStore.by_plate`, plus the same
        `since`/`limit` bound. `LIMIT` is applied against the MOST RECENT
        sightings (DESC then reversed): a long history must truncate its
        oldest end, not its newest — the route an officer asks about is the
        recent one."""
        key = plate_key(raw_plate_text)
        if not key:
            return []
        sql = "SELECT detection_pb FROM correlation_sightings WHERE plate_skeleton = $1"
        args: list = [key]
        if since_s is not None:
            args.append(_ts(since_s))
            sql += f" AND observed_at >= ${len(args)}"
        args.append(limit or 10_000)
        sql += f" ORDER BY observed_at DESC LIMIT ${len(args)}"
        rows: Sequence = await self._pool.fetch(sql, *args)
        return [_row_to_detection(row) for row in reversed(rows)]

    async def unplated_in_range(
        self, start_s: float, end_s: float, *, limit: int | None = None
    ) -> list[events_pb2.VehicleDetection]:
        """Plate-unreadable sightings in `[start_s, end_s]`, oldest first —
        the bridging candidate pool, identical contract to
        `DetectionStore.unplated_between`. Bounded: a busy corridor can put
        thousands of unplated detections inside one gap."""
        rows: Sequence = await self._pool.fetch(
            """
            SELECT detection_pb FROM correlation_sightings
            WHERE plate_skeleton IS NULL AND observed_at BETWEEN $1 AND $2
            ORDER BY observed_at ASC
            LIMIT $3
            """,
            _ts(start_s),
            _ts(end_s),
            limit or 1_000,
        )
        return [_row_to_detection(row) for row in rows]

    async def ping(self) -> bool:
        return bool(await self._pool.fetchval("SELECT 1"))

    async def close(self) -> None:
        await self._pool.close()
