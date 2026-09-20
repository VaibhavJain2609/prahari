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
from .security import hash_api_key, hash_password, hash_session_id, new_api_key, new_session_id


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
        try:
            row = await self._pool.fetchrow(
                "SELECT id, username, org_id, role, created_at, disabled_at "
                "FROM users WHERE id = $1::uuid",
                user_id,
            )
        except (ValueError, asyncpg.DataError):
            # A non-uuid `user_id` is "no such user", not a 500.
            return None
        return _user_from_row(row) if row else None

    async def list_users(self, scope: str) -> list[User]:
        """Every user whose org sits inside `scope`'s ltree subtree — the
        SQL-side version of the `in_scope` check `create_user` applies to a
        single target org, used by the admin user list. `scope` is the
        *caller's* org path; this method never sees a caller-supplied one."""
        rows = await self._pool.fetch(
            """
            SELECT u.id, u.username, u.org_id, u.role, u.created_at, u.disabled_at
            FROM users u
            JOIN orgs o ON o.id = u.org_id
            WHERE o.path <@ $1::ltree
            ORDER BY u.created_at, u.username
            """,
            scope,
        )
        return [_user_from_row(row) for row in rows]

    async def set_disabled(self, user_id: str, *, disabled: bool) -> User | None:
        """Idempotent disable/enable. Disabling keeps the *first*
        `disabled_at` (`COALESCE`), not the latest call's — the timestamp is
        forensic ("when did this account stop working"), and rewriting it on
        every repeated disable would erase exactly that. Returns the updated
        row, or None when no such user exists."""
        if disabled:
            query = (
                "UPDATE users SET disabled_at = COALESCE(disabled_at, now()) "
                "WHERE id = $1::uuid "
                "RETURNING id, username, org_id, role, created_at, disabled_at"
            )
        else:
            query = (
                "UPDATE users SET disabled_at = NULL "
                "WHERE id = $1::uuid "
                "RETURNING id, username, org_id, role, created_at, disabled_at"
            )
        try:
            row = await self._pool.fetchrow(query, user_id)
        except (ValueError, asyncpg.DataError):
            return None
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
    """Sessions keyed by hash, not by the cookie itself.

    `sessions.id` stores `hash_session_id(cookie)` — sha256 truncated into
    the existing uuid column — so a leaked `sessions` row yields no usable
    credential, the same discipline `api_keys.key_hash` already applies to
    API keys. Sessions minted before this scheme (rows whose id is itself
    the cookie) simply stop resolving on deploy; users log in again, which
    is the cheapest migration an ephemeral credential can have.

    Residual risk, flagged for sequencing: the truncation exists only
    because `sessions.id` is `uuid` in migrations/006_identity.sql, which
    the registry owns. A full-width TEXT hash column would be strictly
    better; it needs a registry-side migration, not BFF code.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create(self, user_id: str, *, ttl_hours: int) -> tuple[str, datetime]:
        """Returns `(cookie_value, expires_at)` — the caller sets the cookie
        value; only its hash ever reaches the table."""
        expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours)
        cookie_value = new_session_id()
        await self._pool.execute(
            "INSERT INTO sessions (id, user_id, expires_at) VALUES ($1::uuid, $2::uuid, $3)",
            hash_session_id(cookie_value),
            user_id,
            expires_at,
        )
        return cookie_value, expires_at

    async def resolve(self, session_id: str) -> tuple[User, str] | None:
        """A valid, unexpired, unrevoked session's user and org path in one
        query — a resolver that needed a second round trip to learn scope
        would be a resolver a busy handler forgets to make. `session_id` is
        the cookie value; the lookup is by its hash."""
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
                hash_session_id(session_id),
            )
        except (ValueError, asyncpg.DataError):
            # Belt-and-suspenders: hash_session_id always yields a valid
            # uuid, but a driver-level type error must still read as "no
            # such session", never a 500.
            return None
        if row is None:
            return None
        return _user_from_row(row), row["org_path"]

    async def revoke(self, session_id: str) -> None:
        try:
            await self._pool.execute(
                "UPDATE sessions SET revoked_at = now() WHERE id = $1::uuid",
                hash_session_id(session_id),
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

    async def get(self, key_id: str) -> ApiKey | None:
        """Metadata for one key — `key_hash` is never selected, same
        discipline as `_api_key_from_row` never having a field for it."""
        try:
            row = await self._pool.fetchrow(
                "SELECT id, org_id, role, purpose, label, created_at, last_used_at, revoked_at "
                "FROM api_keys WHERE id = $1::uuid",
                key_id,
            )
        except (ValueError, asyncpg.DataError):
            return None
        return _api_key_from_row(row) if row else None

    async def list_keys(self, scope: str) -> list[ApiKey]:
        """Every key whose org sits inside `scope`'s ltree subtree — the
        admin API-key list. Same scoping rule as `UserRepository.
        list_users`: the subtree predicate is applied in SQL against the
        caller's own org path, never a caller-supplied one."""
        rows = await self._pool.fetch(
            """
            SELECT k.id, k.org_id, k.role, k.purpose, k.label,
                   k.created_at, k.last_used_at, k.revoked_at
            FROM api_keys k
            JOIN orgs o ON o.id = k.org_id
            WHERE o.path <@ $1::ltree
            ORDER BY k.created_at, k.label
            """,
            scope,
        )
        return [_api_key_from_row(row) for row in rows]

    async def revoke(self, key_id: str) -> ApiKey | None:
        """Idempotent revoke: an already-revoked key keeps its *first*
        `revoked_at` (same forensic argument as `UserRepository.
        set_disabled`). Returns the updated row so the endpoint can answer
        with the post-revoke state, or None when no such key exists."""
        try:
            row = await self._pool.fetchrow(
                "UPDATE api_keys SET revoked_at = COALESCE(revoked_at, now()) "
                "WHERE id = $1::uuid "
                "RETURNING id, org_id, role, purpose, label, created_at, "
                "last_used_at, revoked_at",
                key_id,
            )
        except (ValueError, asyncpg.DataError):
            return None
        return _api_key_from_row(row) if row else None
