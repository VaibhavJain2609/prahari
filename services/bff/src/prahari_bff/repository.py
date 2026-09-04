"""Identity storage: users, sessions, API keys — plus the one read-only query
this service makes against `orgs`, which it does not own (the registry does;
see migrations/005_orgs.sql) but must join against to resolve a `Principal`'s
scope and to validate a target org for admin actions.

The BFF never writes `cameras`, `orgs`, or anything else the registry owns —
see docs/ORG-TIERS-DESIGN.md §3.1. Keeping that boundary means every module
in this file is `INSERT`/`SELECT`/`UPDATE` against exactly `users`,
`sessions`, `api_keys`, plus `SELECT ... FROM orgs` for scope resolution.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg

from .models import ApiKey, ApiKeyCreate, Role, User, UserCreate
from .security import hash_api_key, hash_password, new_api_key


def _user_from_row(row: asyncpg.Record) -> User:
    return User(
        id=str(row["id"]),
        username=row["username"],
        org_id=str(row["org_id"]),
        role=Role(row["role"]),
        created_at=row["created_at"],
        disabled_at=row["disabled_at"],
    )


def _api_key_from_row(row: asyncpg.Record) -> ApiKey:
    return ApiKey(
        id=str(row["id"]),
        org_id=str(row["org_id"]),
        role=Role(row["role"]),
        purpose=row["purpose"],
        label=row["label"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        revoked_at=row["revoked_at"],
    )


async def org_path_for_id(pool: asyncpg.Pool, org_id: str) -> str | None:
    """The one read this service makes against a table it does not own. Used
    to resolve a session/api-key principal's scope, and to validate a target
    `org_id` on admin actions (create user, issue key) before writing."""
    return await pool.fetchval("SELECT path::text FROM orgs WHERE id = $1::uuid", org_id)


def in_scope(candidate_path: str, scope_path: str) -> bool:
    """Python-side mirror of the SQL predicate `path <@ scope` (ltree
    containment) — used only for the admin boundary check below, never as a
    substitute for the real `<@` predicate a scoped database read applies.
    ltree paths are dot-separated labels, so containment is exactly "equal,
    or a dotted prefix of the candidate"."""
    return candidate_path == scope_path or candidate_path.startswith(scope_path + ".")


class UserRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create(self, payload: UserCreate) -> User:
        row = await self._pool.fetchrow(
            """
            INSERT INTO users (username, password_hash, org_id, role)
            VALUES ($1, $2, $3::uuid, $4)
            RETURNING id, username, org_id, role, created_at, disabled_at
            """,
            payload.username,
            hash_password(payload.password),
            payload.org_id,
            payload.role.value,
        )
        return _user_from_row(row)

    async def get(self, user_id: str) -> User | None:
        row = await self._pool.fetchrow(
            "SELECT id, username, org_id, role, created_at, disabled_at "
            "FROM users WHERE id = $1::uuid",
            user_id,
        )
        return _user_from_row(row) if row else None

    async def get_by_username_with_hash(self, username: str) -> tuple[User, str] | None:
        """Returns (User, password_hash). The hash never leaves this method —
        every other caller in this codebase gets a `User` with no such field,
        the same discipline `camera_from_row` applies to `stream_secret`."""
        row = await self._pool.fetchrow(
            "SELECT id, username, org_id, role, created_at, disabled_at, password_hash "
            "FROM users WHERE username = $1",
            username,
        )
        if row is None:
            return None
        return _user_from_row(row), row["password_hash"]

    async def count(self) -> int:
        return await self._pool.fetchval("SELECT count(*) FROM users")


class SessionRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create(self, user_id: str, *, ttl_hours: int) -> tuple[str, datetime]:
        expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours)
        session_id = await self._pool.fetchval(
            "INSERT INTO sessions (user_id, expires_at) VALUES ($1::uuid, $2) RETURNING id",
            user_id,
            expires_at,
        )
        return str(session_id), expires_at

    async def resolve(self, session_id: str) -> tuple[User, str] | None:
        """A valid, unexpired, unrevoked session's user and org path in one
        query — a resolver that needed a second round trip to learn scope
        would be a resolver a busy handler forgets to make."""
        try:
            row = await self._pool.fetchrow(
                """
                SELECT u.id, u.username, u.org_id, u.role, u.created_at, u.disabled_at,
                       o.path::text AS org_path
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                JOIN orgs o ON o.id = u.org_id
                WHERE s.id = $1::uuid
                  AND s.revoked_at IS NULL
                  AND s.expires_at > now()
                  AND u.disabled_at IS NULL
                """,
                session_id,
            )
        except (ValueError, asyncpg.DataError):
            # Not a well-formed uuid — a forged or stale cookie, not a server error.
            return None
        if row is None:
            return None
        return _user_from_row(row), row["org_path"]

    async def revoke(self, session_id: str) -> None:
        try:
            await self._pool.execute(
                "UPDATE sessions SET revoked_at = now() WHERE id = $1::uuid", session_id
            )
        except (ValueError, asyncpg.DataError):
            return


class ApiKeyRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create(self, payload: ApiKeyCreate, *, created_by: str | None) -> tuple[ApiKey, str]:
        plaintext, key_hash = new_api_key()
        row = await self._pool.fetchrow(
            """
            INSERT INTO api_keys (key_hash, org_id, role, purpose, label, created_by)
            VALUES ($1, $2::uuid, $3, $4, $5, $6::uuid)
            RETURNING id, org_id, role, purpose, label, created_at, last_used_at, revoked_at
            """,
            key_hash,
            payload.org_id,
            payload.role.value,
            payload.purpose.value,
            payload.label,
            created_by,
        )
        return _api_key_from_row(row), plaintext

    async def resolve(self, plaintext: str) -> tuple[ApiKey, str] | None:
        """Looked up by hash, never by a `LIKE` or prefix scan — the plaintext
        never touches a WHERE clause. Touches `last_used_at` on every
        successful resolve so a revoked-but-still-used key is visible in the
        admin screen before it does damage, not just after."""
        row = await self._pool.fetchrow(
            """
            SELECT k.id, k.org_id, k.role, k.purpose, k.label,
                   k.created_at, k.last_used_at, k.revoked_at,
                   o.path::text AS org_path
            FROM api_keys k
            JOIN orgs o ON o.id = k.org_id
            WHERE k.key_hash = $1 AND k.revoked_at IS NULL
            """,
            hash_api_key(plaintext),
        )
        if row is None:
            return None
        await self._pool.execute(
            "UPDATE api_keys SET last_used_at = now() WHERE id = $1", row["id"]
        )
        return _api_key_from_row(row), row["org_path"]

    async def revoke(self, key_id: str) -> None:
        try:
            await self._pool.execute(
                "UPDATE api_keys SET revoked_at = now() WHERE id = $1::uuid", key_id
            )
        except (ValueError, asyncpg.DataError):
            return
