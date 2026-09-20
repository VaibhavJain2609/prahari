"""db.py: the alert-history migration runner and pool construction.

No real Postgres — same posture as `services/correlation/tests/test_db.py`:
`apply_migrations` runs against a recording fake connection/pool, so what is
tested for real is the part that can silently drift — which statements run,
in which order, under which lock, and that an edited applied migration is
rejected rather than ignored.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import prahari_match.db as db_module
from prahari_match.config import MatchSettings
from prahari_match.db import (
    _MIGRATION_LOCK_ID,
    apply_migrations,
    create_pool,
    _init_connection,
    _migration_files,
    _resolve_migrations_dir,
)


# --- fakes -------------------------------------------------------------------


class _Tx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self):
        self._conn.tx_depth += 1
        return self

    async def __aexit__(self, *exc):
        self._conn.tx_depth -= 1
        return False


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    """The slice of asyncpg.Connection `apply_migrations`/`_init_connection`
    use: `execute` (records SQL + the schema_migration ledger), `fetchval`,
    `transaction`, `set_type_codec`."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.applied: dict[str, str] = {}  # version -> checksum, the ledger
        self.tx_depth = 0
        self.tx_bound_statements: list[str] = []
        self.codecs: list[tuple] = []

    async def execute(self, sql: str, *args) -> None:
        self.executed.append((sql, args))
        if self.tx_depth:
            self.tx_bound_statements.append(sql)
        if sql.startswith("INSERT INTO schema_migration"):
            self.applied[args[0]] = args[1]

    async def fetchval(self, sql: str, *args):
        return self.applied.get(args[0])

    def transaction(self):
        return _Tx(self)

    async def set_type_codec(self, name, *, encoder, decoder, schema):  # noqa: ANN001, ANN202
        self.codecs.append((name, encoder, decoder, schema))


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


# --- migrations-dir resolution -------------------------------------------------


def test_resolve_migrations_dir_prefers_the_packaged_copy(tmp_path, monkeypatch) -> None:
    # Inside a built wheel the migrations sit next to the module (the
    # force-include in pyproject.toml); the service-root fallback is for the
    # editable checkout only.
    pkg = tmp_path / "prahari_match"
    (pkg / "migrations").mkdir(parents=True)
    monkeypatch.setattr(db_module, "__file__", str(pkg / "db.py"))
    assert _resolve_migrations_dir() == pkg / "migrations"


def test_resolve_migrations_dir_falls_back_to_the_service_root() -> None:
    # The editable checkout this suite runs in: src/prahari_match/migrations
    # does not exist, so resolution must land on services/match-engine/migrations.
    resolved = _resolve_migrations_dir()
    assert resolved.name == "migrations"
    assert (resolved / "0001_alerts.sql").is_file()


def test_migration_files_raise_for_a_missing_directory(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="migrations directory not found"):
        _migration_files(tmp_path / "nowhere")


def test_migration_files_returns_sorted_sql_only(tmp_path) -> None:
    (tmp_path / "002_b.sql").write_text("SELECT 2;")
    (tmp_path / "001_a.sql").write_text("SELECT 1;")
    (tmp_path / "notes.txt").write_text("not a migration")
    assert [p.name for p in _migration_files(tmp_path)] == ["001_a.sql", "002_b.sql"]


# --- pool construction ---------------------------------------------------------


async def test_init_connection_registers_the_jsonb_codec() -> None:
    conn = _FakeConn()
    await _init_connection(conn)  # type: ignore[arg-type] -- fake conn
    (name, encoder, decoder, schema), = conn.codecs
    assert name == "jsonb" and schema == "pg_catalog"
    # asyncpg returns jsonb as a str otherwise; the codec must actually be
    # json.loads/json.dumps, not a passthrough.
    assert decoder(encoder({"a": 1})) == {"a": 1}
    assert encoder("x") == json.dumps("x")


async def test_create_pool_passes_settings_and_the_codec_init(monkeypatch) -> None:
    captured: dict = {}

    async def _fake_create_pool(url, **kwargs):  # noqa: ANN001, ANN202
        captured["url"] = url
        captured.update(kwargs)
        return "POOL"

    monkeypatch.setattr(db_module.asyncpg, "create_pool", _fake_create_pool)
    settings = MatchSettings(
        database_url="postgresql://x/db", db_pool_min=2, db_pool_max=7
    )

    assert await create_pool(settings) == "POOL"
    assert captured["url"] == "postgresql://x/db"
    assert (captured["min_size"], captured["max_size"]) == (2, 7)
    assert captured["init"] is _init_connection
    # The connect timeout is what keeps a down Postgres from hanging startup
    # instead of degrading to in-memory history (app.py's fallback).
    assert captured["command_timeout"] == 30.0


# --- the migration runner --------------------------------------------------------


def _write_migrations(tmp_path: Path) -> None:
    (tmp_path / "001_first.sql").write_text("CREATE TABLE one (id int);")
    (tmp_path / "002_second.sql").write_text("CREATE TABLE two (id int);")


async def test_migrations_apply_in_order_under_the_advisory_lock(tmp_path) -> None:
    _write_migrations(tmp_path)
    conn = _FakeConn()

    applied = await apply_migrations(_FakePool(conn), directory=tmp_path)  # type: ignore[arg-type]

    assert applied == ["001_first", "002_second"]
    # Advisory lock wrapped the whole sequence, and was released even on the
    # success path — a leaked lock would wedge every other replica's startup.
    lock_calls = [sql for sql, _a in conn.executed if "pg_advisory_lock" in sql]
    unlock_calls = [sql for sql, _a in conn.executed if "pg_advisory_unlock" in sql]
    assert len(lock_calls) == len(unlock_calls) == 1
    assert conn.executed.index((lock_calls[0], (_MIGRATION_LOCK_ID,))) < next(
        i for i, (sql, _a) in enumerate(conn.executed) if "CREATE TABLE one" in sql
    )
    # Each migration's DDL + ledger insert ran inside a transaction — a
    # half-applied file must roll back together.
    assert "CREATE TABLE one (id int);" in conn.tx_bound_statements
    assert sum("INSERT INTO schema_migration" in s for s in conn.tx_bound_statements) == 2
    # The ledger rows carry the files' real checksums.
    for name in ("001_first", "002_second"):
        expected = hashlib.sha256((tmp_path / f"{name}.sql").read_bytes()).hexdigest()
        assert conn.applied[name] == expected


async def test_already_applied_migrations_are_skipped(tmp_path) -> None:
    _write_migrations(tmp_path)
    conn = _FakeConn()
    conn.applied["001_first"] = hashlib.sha256(
        (tmp_path / "001_first.sql").read_bytes()
    ).hexdigest()

    applied = await apply_migrations(_FakePool(conn), directory=tmp_path)  # type: ignore[arg-type]

    assert applied == ["002_second"]  # the applied one was not re-run
    # The migration DDL that ran is exactly file 002's body — 001's was skipped.
    migration_ddl = [sql for sql, _a in conn.executed if sql.endswith("(id int);")]
    assert migration_ddl == ["CREATE TABLE two (id int);"]


async def test_a_second_run_applies_nothing(tmp_path) -> None:
    _write_migrations(tmp_path)
    conn = _FakeConn()
    pool = _FakePool(conn)

    await apply_migrations(pool, directory=tmp_path)  # type: ignore[arg-type]
    assert await apply_migrations(pool, directory=tmp_path) == []  # type: ignore[arg-type]


async def test_a_changed_applied_migration_is_rejected(tmp_path) -> None:
    _write_migrations(tmp_path)
    conn = _FakeConn()
    conn.applied["001_first"] = "0" * 64  # recorded checksum != file checksum

    with pytest.raises(RuntimeError, match="001_first has changed"):
        await apply_migrations(_FakePool(conn), directory=tmp_path)  # type: ignore[arg-type]

    # The lock was still released — rejection must not wedge other replicas.
    assert any("pg_advisory_unlock" in sql for sql, _a in conn.executed)


async def test_migrations_apply_against_the_real_directory() -> None:
    """The shipped migrations directory parses through the runner itself —
    catches a broken MIGRATIONS_DIR resolution or an unreadable .sql the
    same way a fresh deploy would hit it."""
    conn = _FakeConn()
    applied = await apply_migrations(_FakePool(conn))  # type: ignore[arg-type]
    assert applied == ["0001_alerts"]
    assert "0001_alerts" in conn.applied
