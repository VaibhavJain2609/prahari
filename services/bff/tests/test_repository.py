"""The identity repository layer, tested without a database: the ltree
containment mirror used for admin boundary checks, the one read this service
makes against a table it does not own, and every repository class against a
scripted `asyncpg.Pool` double.

The fake pool is deliberately dumb — it records the query and returns the
row(s) the test handed it. What is under test is the repository's own logic:
which statement it issues, how it maps a row to a model, and that a
driver-level type error reads as "no such row" rather than a 500.
"""

from __future__ import annotations

from datetime import UTC, datetime

import asyncpg

from prahari_bff.models import ApiKeyCreate, ApiKeyPurpose, Role, UserCreate
from prahari_bff.repository import (
    ApiKeyRepository,
    SessionRepository,
    UserRepository,
    in_scope,
    org_path_for_id,
)
from prahari_bff.security import hash_session_id


def test_in_scope_same_path():
    assert in_scope("gj.ahmedabad_city", "gj.ahmedabad_city")


def test_in_scope_descendant():
    assert in_scope("gj.ahmedabad_city.zone_4", "gj.ahmedabad_city")


def test_in_scope_rejects_ancestor():
    """Being an ancestor of the scope is not being inside it — a zone-4 admin
    must not be able to name the state root as a "target" org."""
    assert not in_scope("gj", "gj.ahmedabad_city")


def test_in_scope_rejects_sibling():
    assert not in_scope("gj.surat_city", "gj.ahmedabad_city")


def test_in_scope_rejects_label_prefix_collision():
    """A naive `str.startswith(scope)` with no dot boundary would wrongly let
    'gj.ahmedabad_city_east' pass as inside scope 'gj.ahmedabad_city' — ltree
    containment is per-label, not per-character."""
    assert not in_scope("gj.ahmedabad_city_east", "gj.ahmedabad_city")


class FakePool:
    def __init__(self, paths: dict[str, str]) -> None:
        self._paths = paths

    async def fetchval(self, query: str, org_id: str) -> str | None:
        assert "FROM orgs WHERE id" in query
        return self._paths.get(org_id)


async def test_org_path_for_id_found():
    pool = FakePool({"org-1": "gj.ahmedabad_city"})
    assert await org_path_for_id(pool, "org-1") == "gj.ahmedabad_city"


async def test_org_path_for_id_missing():
    pool = FakePool({})
    assert await org_path_for_id(pool, "org-x") is None


# --- scripted pool ---------------------------------------------------------
#
# asyncpg.Record is only ever subscripted by column name in repository.py, so
# a plain dict is an honest stand-in for a row.


def _user_row(**overrides) -> dict:
    row = {
        "id": "00000000-0000-0000-0000-0000000000u1",
        "username": "ops.zone4",
        "org_id": "org-zone4",
        "role": "operator",
        "created_at": datetime(2026, 9, 1, tzinfo=UTC),
        "disabled_at": None,
    }
    row.update(overrides)
    return row


def _api_key_row(**overrides) -> dict:
    row = {
        "id": "k1",
        "org_id": "org-zone4",
        "role": "viewer",
        "purpose": "vendor_adapter",
        "label": "vendor-x",
        "created_at": datetime(2026, 9, 1, tzinfo=UTC),
        "last_used_at": None,
        "revoked_at": None,
    }
    row.update(overrides)
    return row


class ScriptedPool:
    """Returns scripted results and records every call. `error` is raised by
    whichever method the test exercises, standing in for asyncpg's
    DataError/ValueError on a malformed literal."""

    def __init__(
        self,
        *,
        row: dict | None = None,
        rows: list[dict] | None = None,
        value=None,
        error: Exception | None = None,
    ) -> None:
        self._row = row
        self._rows = rows or []
        self._value = value
        self._error = error
        self.calls: list[tuple[str, str, tuple]] = []

    def _maybe_raise(self):
        if self._error is not None:
            raise self._error

    async def fetchrow(self, query: str, *args):
        self.calls.append(("fetchrow", query, args))
        self._maybe_raise()
        return self._row

    async def fetch(self, query: str, *args):
        self.calls.append(("fetch", query, args))
        self._maybe_raise()
        return self._rows

    async def fetchval(self, query: str, *args):
        self.calls.append(("fetchval", query, args))
        self._maybe_raise()
        return self._value

    async def execute(self, query: str, *args):
        self.calls.append(("execute", query, args))
        self._maybe_raise()
        return "UPDATE 1"


# --- UserRepository ----------------------------------------------------------


async def test_user_create_inserts_a_hash_never_the_plaintext():
    pool = ScriptedPool(row=_user_row())
    repo = UserRepository(pool)
    payload = UserCreate(
        username="ops.zone4", password="long-enough-password", org_id="org-zone4", role="operator"
    )
    user = await repo.create(payload)
    assert user.username == "ops.zone4"
    assert user.role == Role.OPERATOR
    _, query, args = pool.calls[0]
    assert "INSERT INTO users" in query
    assert args[0] == "ops.zone4"
    assert args[1] != "long-enough-password"  # argon2 hash, not the plaintext
    assert args[2:] == ("org-zone4", "operator")


async def test_user_get_maps_the_row():
    repo = UserRepository(ScriptedPool(row=_user_row(disabled_at=datetime(2026, 9, 2, tzinfo=UTC))))
    user = await repo.get("u1")
    assert user is not None
    assert user.id == "00000000-0000-0000-0000-0000000000u1"
    assert user.disabled_at is not None


async def test_user_get_missing_is_none():
    assert await UserRepository(ScriptedPool()).get("ghost") is None


async def test_user_get_with_a_malformed_id_is_none_not_a_500():
    """asyncpg raises DataError on a non-uuid `$1::uuid` literal — a caller
    error the repo must read as 'no such user'."""
    assert (
        await UserRepository(ScriptedPool(error=asyncpg.DataError("bad uuid"))).get("not-a-uuid")
        is None
    )


async def test_user_list_scopes_by_ltree_subtree():
    pool = ScriptedPool(rows=[_user_row(username="a"), _user_row(username="b")])
    users = await UserRepository(pool).list_users("gj.ahmedabad_city")
    assert [u.username for u in users] == ["a", "b"]
    _, query, args = pool.calls[0]
    assert "path <@ $1::ltree" in query
    assert args == ("gj.ahmedabad_city",)


async def test_user_set_disabled_both_directions():
    pool = ScriptedPool(row=_user_row(disabled_at=datetime(2026, 9, 2, tzinfo=UTC)))
    repo = UserRepository(pool)
    user = await repo.set_disabled("u1", disabled=True)
    assert user.disabled_at is not None
    assert "COALESCE(disabled_at, now())" in pool.calls[0][1]

    pool._row = _user_row()
    user = await repo.set_disabled("u1", disabled=False)
    assert user.disabled_at is None
    assert "disabled_at = NULL" in pool.calls[1][1]


async def test_user_set_disabled_missing_or_malformed_is_none():
    assert await UserRepository(ScriptedPool()).set_disabled("ghost", disabled=True) is None
    assert (
        await UserRepository(ScriptedPool(error=asyncpg.DataError("bad uuid"))).set_disabled(
            "junk", disabled=False
        )
        is None
    )


async def test_get_by_username_with_hash_returns_user_and_hash():
    pool = ScriptedPool(row=_user_row(password_hash="argon2-hash"))
    resolved = await UserRepository(pool).get_by_username_with_hash("ops.zone4")
    assert resolved is not None
    user, password_hash = resolved
    assert user.username == "ops.zone4"
    assert password_hash == "argon2-hash"


async def test_get_by_username_missing_is_none():
    repo = UserRepository(ScriptedPool())
    assert await repo.get_by_username_with_hash("ghost") is None


async def test_user_count():
    assert await UserRepository(ScriptedPool(value=7)).count() == 7


# --- SessionRepository ---------------------------------------------------------


async def test_session_create_stores_the_hash_and_returns_the_cookie():
    pool = ScriptedPool()
    repo = SessionRepository(pool)
    cookie, expires_at = await repo.create("u1", ttl_hours=12)
    assert expires_at > datetime.now(UTC)
    _, query, args = pool.calls[0]
    assert "INSERT INTO sessions" in query
    # The table sees hash_session_id(cookie), never the cookie itself.
    assert args[0] == hash_session_id(cookie)
    assert cookie not in args


async def test_session_resolve_returns_user_and_org_path():
    pool = ScriptedPool(row=_user_row(org_path="gj.ahmedabad_city.zone_4"))
    resolved = await SessionRepository(pool).resolve("cookie-value")
    assert resolved is not None
    user, org_path = resolved
    assert user.username == "ops.zone4"
    assert org_path == "gj.ahmedabad_city.zone_4"
    _, query, args = pool.calls[0]
    # Resolved by hash — the cookie never reaches the WHERE clause.
    assert args == (hash_session_id("cookie-value"),)


async def test_session_resolve_miss_and_driver_error_are_none():
    assert await SessionRepository(ScriptedPool()).resolve("cookie") is None
    assert (
        await SessionRepository(ScriptedPool(error=asyncpg.DataError("x"))).resolve("cookie")
        is None
    )


async def test_session_revoke_swallows_driver_errors():
    pool = ScriptedPool()
    await SessionRepository(pool).revoke("cookie-value")
    _, query, args = pool.calls[0]
    assert "revoked_at = now()" in query
    assert args == (hash_session_id("cookie-value"),)
    # A driver error is deliberately swallowed — revoke is best-effort.
    await SessionRepository(ScriptedPool(error=asyncpg.DataError("x"))).revoke("cookie")


# --- ApiKeyRepository ------------------------------------------------------------


async def test_api_key_create_returns_metadata_and_the_one_time_plaintext():
    pool = ScriptedPool(row=_api_key_row())
    repo = ApiKeyRepository(pool)
    payload = ApiKeyCreate(
        org_id="org-zone4",
        role=Role.VIEWER,
        purpose=ApiKeyPurpose.VENDOR_ADAPTER,
        label="vendor-x",
    )
    key, plaintext = await repo.create(payload, created_by="u0")
    assert key.label == "vendor-x"
    assert key.purpose == ApiKeyPurpose.VENDOR_ADAPTER
    assert plaintext.startswith("pk_")
    _, query, args = pool.calls[0]
    assert "INSERT INTO api_keys" in query
    assert plaintext not in args  # the hash is stored, not the key


async def test_api_key_resolve_touches_last_used_at_on_hit():
    pool = ScriptedPool(row=_api_key_row(org_path="gj.ahmedabad_city.zone_4"))
    resolved = await ApiKeyRepository(pool).resolve("pk_plaintext")
    assert resolved is not None
    key, org_path = resolved
    assert key.id == "k1"
    assert org_path == "gj.ahmedabad_city.zone_4"
    kinds = [kind for kind, _, _ in pool.calls]
    assert kinds == ["fetchrow", "execute"]  # resolve, then last_used_at touch
    _, update, args = pool.calls[1]
    assert "last_used_at = now()" in update
    assert args == ("k1",)


async def test_api_key_resolve_miss_is_none():
    pool = ScriptedPool()
    assert await ApiKeyRepository(pool).resolve("pk_nope") is None
    assert [kind for kind, _, _ in pool.calls] == ["fetchrow"]  # no touch on a miss


async def test_api_key_get_found_missing_and_malformed():
    repo = ApiKeyRepository(ScriptedPool(row=_api_key_row()))
    key = await repo.get("k1")
    assert key is not None and key.label == "vendor-x"
    assert await ApiKeyRepository(ScriptedPool()).get("ghost") is None
    assert (
        await ApiKeyRepository(ScriptedPool(error=asyncpg.DataError("bad uuid"))).get("junk")
        is None
    )


async def test_api_key_list_scopes_by_ltree_subtree():
    pool = ScriptedPool(rows=[_api_key_row(label="a"), _api_key_row(label="b")])
    keys = await ApiKeyRepository(pool).list_keys("gj")
    assert [k.label for k in keys] == ["a", "b"]
    _, query, args = pool.calls[0]
    assert "path <@ $1::ltree" in query
    assert args == ("gj",)


async def test_api_key_revoke_returns_the_post_revoke_row():
    pool = ScriptedPool(row=_api_key_row(revoked_at=datetime(2026, 9, 3, tzinfo=UTC)))
    key = await ApiKeyRepository(pool).revoke("k1")
    assert key is not None and key.revoked_at is not None
    assert "COALESCE(revoked_at, now())" in pool.calls[0][1]


async def test_api_key_revoke_missing_or_malformed_is_none():
    assert await ApiKeyRepository(ScriptedPool()).revoke("ghost") is None
    assert (
        await ApiKeyRepository(ScriptedPool(error=asyncpg.DataError("bad uuid"))).revoke("junk")
        is None
    )
