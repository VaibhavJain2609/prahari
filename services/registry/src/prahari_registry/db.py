"""Database pool and migrations.

The service applies its own schema on startup. A separate migration Job would
mean `helm install` has an ordering constraint and a second thing to get right
on demo day; applying in-process means the registry is either up with a correct
schema or not up at all.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import asyncpg

from .config import RegistrySettings

log = logging.getLogger(__name__)

# Any 64-bit constant. Guards the migration sequence when more than one replica
# starts at once — both will try, one will wait, neither will half-apply.
_MIGRATION_LOCK_ID = 0x5052_4148_4152_49  # "PRAHARI"


def _resolve_migrations_dir() -> Path:
    """Where the .sql files live.

    Inside a built wheel they sit next to the package (see the force-include in
    pyproject). In an editable checkout the package is under `src/` and the
    migrations are two levels up at the service root. Checking both means the
    same code path runs in the container and on the laptop — a migration runner
    that only works in one of them is a migration runner that gets tested in
    neither.
    """
    packaged = Path(__file__).parent / "migrations"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "migrations"


MIGRATIONS_DIR = _resolve_migrations_dir()


async def _init_connection(conn: asyncpg.Connection) -> None:
    """asyncpg returns jsonb as a string unless told otherwise."""
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(settings: RegistrySettings) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        init=_init_connection,
        # A pod that cannot reach Postgres should fail its readiness probe and be
        # restarted, not hang holding a connection attempt open.
        command_timeout=30.0,
    )


def _migration_files(directory: Path | None = None) -> list[Path]:
    d = directory or MIGRATIONS_DIR
    if not d.is_dir():
        raise FileNotFoundError(f"migrations directory not found: {d}")
    return sorted(d.glob("*.sql"))


async def apply_migrations(pool: asyncpg.Pool, directory: Path | None = None) -> list[str]:
    """Apply every unapplied migration, in filename order. Returns what ran.

    Applied migrations are checksummed. Editing a file that has already run is
    rejected rather than ignored: it is the failure that leaves the laptop and
    the cloud on different schemas while both report success, and it surfaces on
    demo day as a column that does not exist.
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


async def timescale_available(pool: asyncpg.Pool) -> bool:
    """Whether the health time-series is a hypertable or a plain table.

    Reported on /healthz rather than assumed, because the degraded path is
    silent by design: queries behave identically, and only retention quietly
    stops happening.
    """
    return bool(
        await pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')"
        )
    )
