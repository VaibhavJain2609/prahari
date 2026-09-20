"""Postgres pool and migrations for alert history.

The service applies its own schema on startup, for the same reason the
registry does (`prahari_registry.db` — copied here, not imported: two
deployables must not share a Python import across the service boundary, or a
schema helper becomes an undeclared coupling). A separate migration Job would
give `helm install` an ordering constraint; applying in-process means the
match engine is either up with a correct schema or — unlike the registry —
degraded to in-memory history, since a Postgres outage must not take the live
alert relay down with it (see app.py's lifespan).
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import asyncpg

from .config import MatchSettings

log = logging.getLogger(__name__)

# Any 64-bit constant; distinct from the registry's so the two services can
# migrate concurrently when they share a database. Guards this service's
# migration sequence when more than one match-engine replica starts at once.
_MIGRATION_LOCK_ID = 0x5052_4148_4152_4D45  # "PRAHARME"


def _resolve_migrations_dir() -> Path:
    """Where the .sql files live — same dual-path trick as the registry's.

    Inside a built wheel they sit next to the package (see the force-include
    in pyproject.toml). In an editable checkout the package is under `src/`
    and the migrations are two levels up at the service root.
    """
    packaged = Path(__file__).parent / "migrations"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "migrations"


MIGRATIONS_DIR = _resolve_migrations_dir()


async def _init_connection(conn: asyncpg.Connection) -> None:
    """asyncpg returns jsonb as a string unless told otherwise."""
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(settings: MatchSettings) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        init=_init_connection,
        # A pod that cannot reach Postgres should degrade to in-memory history
        # (app.py catches and falls back), not hang holding a connect attempt.
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
    than ignored — it is the failure that leaves the laptop and the cloud on
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
