"""The hash-chained audit log.

Append-only SQLite, deliberately not the shared Postgres (see
`BFFSettings.audit_db_path`, and `docs/ORG-TIERS-DESIGN.md §4`, which mirrors
`docs/DAY3-DESIGN.md §4.2`'s original reasoning: single-writer, append-only,
never joined against anything). Every video/route/camera-detail access is one
entry here, denials included — a cross-org access attempt is itself an audit
entry (`action="denied"`), not silently dropped.

    entry = {id, actor, org_path, purpose_code, resource, action, occurred_at, prev_hash}
    hash  = sha256(canonical_json(entry) + prev_hash)

`canonical_json` is `json.dumps(..., sort_keys=True, separators=(",", ":"))`
so the hash does not depend on dict insertion order. The genesis link is a
fixed 64 `"0"` characters, so `verify()` never has to special-case entry #1.

sqlite3 is synchronous; every public method here wraps its call in
`asyncio.to_thread` so a slow disk does not stall the event loop that is also
serving the SSE relay.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

__all__ = ["AuditEntry", "AuditLog"]

_GENESIS_HASH = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    org_path TEXT NOT NULL,
    purpose_code TEXT NOT NULL,
    resource TEXT NOT NULL,
    action TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class AuditEntry:
    id: int
    actor: str
    org_path: str
    purpose_code: str
    resource: str
    action: str
    occurred_at: str
    prev_hash: str
    hash: str


def _canonical_json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _entry_hash(entry: dict, prev_hash: str) -> str:
    return hashlib.sha256((_canonical_json(entry) + prev_hash).encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    async def append(
        self, *, actor: str, org_path: str, purpose_code: str, resource: str, action: str
    ) -> AuditEntry:
        return await asyncio.to_thread(
            self._append_sync, actor, org_path, purpose_code, resource, action
        )

    def _append_sync(
        self, actor: str, org_path: str, purpose_code: str, resource: str, action: str
    ) -> AuditEntry:
        cur = self._conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        prev_hash = row[0] if row else _GENESIS_HASH
        occurred_at = datetime.now(UTC).isoformat()

        # `id` is not known until the INSERT assigns it, so it is not part of
        # what gets hashed -- the hash covers exactly the fields a verifier
        # can recompute from the row it just read, `id` included only as the
        # row's own identity, never as chain input.
        payload = {
            "actor": actor,
            "org_path": org_path,
            "purpose_code": purpose_code,
            "resource": resource,
            "action": action,
            "occurred_at": occurred_at,
        }
        entry_hash = _entry_hash(payload, prev_hash)

        cur = self._conn.execute(
            """
            INSERT INTO audit_log
                (actor, org_path, purpose_code, resource, action, occurred_at, prev_hash, hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (actor, org_path, purpose_code, resource, action, occurred_at, prev_hash, entry_hash),
        )
        self._conn.commit()
        return AuditEntry(id=cur.lastrowid, prev_hash=prev_hash, hash=entry_hash, **payload)

    async def verify(self) -> tuple[bool, int | None]:
        """Walks the chain in id order and returns `(ok, first_broken_id)`.
        `first_broken_id` is the id whose stored hash does not match what
        recomputing from its own fields plus the previous row's hash
        produces — either the row was tampered with, or an earlier row was,
        which breaks every hash after it."""
        return await asyncio.to_thread(self._verify_sync)

    def _verify_sync(self) -> tuple[bool, int | None]:
        cur = self._conn.execute(
            "SELECT id, actor, org_path, purpose_code, resource, action, occurred_at, "
            "prev_hash, hash FROM audit_log ORDER BY id ASC"
        )
        expected_prev = _GENESIS_HASH
        for row in cur.fetchall():
            (
                entry_id,
                actor,
                org_path,
                purpose_code,
                resource,
                action,
                occurred_at,
                prev_hash,
                stored_hash,
            ) = row
            if prev_hash != expected_prev:
                return False, entry_id
            payload = {
                "actor": actor,
                "org_path": org_path,
                "purpose_code": purpose_code,
                "resource": resource,
                "action": action,
                "occurred_at": occurred_at,
            }
            if _entry_hash(payload, prev_hash) != stored_hash:
                return False, entry_id
            expected_prev = stored_hash
        return True, None

    async def recent(self, limit: int = 100) -> list[AuditEntry]:
        return await asyncio.to_thread(self._recent_sync, limit)

    def _recent_sync(self, limit: int) -> list[AuditEntry]:
        cur = self._conn.execute(
            "SELECT id, actor, org_path, purpose_code, resource, action, occurred_at, "
            "prev_hash, hash FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [AuditEntry(*row) for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()
