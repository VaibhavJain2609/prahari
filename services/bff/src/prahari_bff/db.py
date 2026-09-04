"""Database pool.

Deliberately self-contained rather than importing `prahari_registry.db` — the
registry and the BFF are separate deployables that happen to share a
database, and a Python import between two services would smuggle in a
coupling the HTTP/protobuf boundary is supposed to make explicit. The BFF
never runs migrations: the registry's checksummed, advisory-locked runner
(`prahari_registry.db.apply_migrations`) owns every table in this database,
identity included — see `migrations/006_identity.sql`'s header comment.
"""

from __future__ import annotations

import asyncpg

from .config import BFFSettings


async def create_pool(settings: BFFSettings) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        # A pod that cannot reach Postgres should fail readiness and be
        # restarted, not hang holding a connection attempt open.
        command_timeout=30.0,
    )
