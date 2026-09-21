"""`db.py` — pool construction, the migration runner, and the TimescaleDB
probe, all against scripted fakes.

`apply_migrations` is the piece with real logic worth pinning: ordering by
filename, checksum rejection of an edited applied migration, and the advisory
lock/unlock around the sequence. No test here touches Postgres — the fake
connection records statements and answers `schema_migration` lookups from an
in-memory dict, which is what the runner's contract actually is.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import asyncpg
import pytest

from prahari_registry import db
from prahari_registry.config import RegistrySettings
from prahari_registry.db import (
    _init_connection,
    _migration_files,
    _resolve_migrations_dir,
    apply_migrations,
    create_pool,
    timescale_available,
)


class FakeConn:
    """Records every statement; answers `schema_migration` lookups from
    `applied`, which the runner's own INSERT populates."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple]] = []
        self.applied: dict[str, str] = {}
        self.unlocked = False
        self.type_codecs: list[dict] = []

    async def execute(self, sql: str, *args) -> str:
        self.statements.append((sql, args))
        if "pg_advisory_unlock" in sql:
            self.unlocked = True
        if "INSERT INTO schema_migration" in sql:
            self.applied[args[0]] = args[1]
        return "OK"

    async def fetchval(self, sql: str, *args):
        assert "FROM schema_migration" in sql
        return self.applied.get(args[0])

    async def set_type_codec(self, name: str, **kwargs) -> None:
        self.type_codecs.append({"name": name, **kwargs})

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    def acquire(self) -> FakeConn:
        return self.conn


def _write_migrations(directory: Path, files: dict[str, str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, sql in files.items():
        (directory / name).write_text(sql, encoding="utf-8")


# --- migrations dir resolution ------------------------------------------------


def test_migrations_dir_prefers_the_packaged_copy(monkeypatch, tmp_path):
    """Inside a built wheel the .sql files sit next to the package (the
    force-include in pyproject); that copy wins when it exists."""
    packaged = tmp_path / "pkg" / "migrations"
    packaged.mkdir(parents=True)
    monkeypatch.setattr(db, "__file__", str(tmp_path / "pkg" / "db.py"))
    assert _resolve_migrations_dir() == packaged


def test_migrations_dir_falls_back_to_the_checkout_layout(monkeypatch, tmp_path):
    """Editable checkout: src/prahari_registry/db.py → service root is two
    parents up. Same code path in the container and on the laptop."""
    fake_file = tmp_path / "svc" / "src" / "pkg" / "db.py"
    fake_file.parent.mkdir(parents=True)
    monkeypatch.setattr(db, "__file__", str(fake_file))
    assert _resolve_migrations_dir() == tmp_path / "svc" / "migrations"


def test_default_migrations_dir_resolves_to_the_shipped_sql():
    assert db.MIGRATIONS_DIR.is_dir()
    assert _migration_files()  # the service has real migrations


def test_migration_files_require_the_directory(tmp_path):
    with pytest.raises(FileNotFoundError):
        _migration_files(tmp_path / "nonexistent")


def test_migration_files_sort_by_name(tmp_path):
    _write_migrations(tmp_path, {"002_b.sql": "SELECT 2", "001_a.sql": "SELECT 1"})
    assert [p.stem for p in _migration_files(tmp_path)] == ["001_a", "002_b"]


# --- pool construction ----------------------------------------------------------


async def test_init_connection_registers_the_jsonb_codec():
    """asyncpg returns jsonb as str unless told otherwise — a regression here
    turns `cameras.raw` and `codec_mix` into strings everywhere downstream."""
    conn = FakeConn()
    await _init_connection(conn)
    assert conn.type_codecs == [
        {
            "name": "jsonb",
            "encoder": json.dumps,
            "decoder": json.loads,
            "schema": "pg_catalog",
        }
    ]


async def test_create_pool_passes_the_settings_through(monkeypatch):
    captured: dict = {}

    async def fake_create_pool(dsn, **kwargs):
        captured["dsn"] = dsn
        captured.update(kwargs)
        return "the-pool"

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)
    settings = RegistrySettings(
        database_url="postgresql://example/prahari", db_pool_min=1, db_pool_max=4
    )
    assert await create_pool(settings) == "the-pool"
    assert captured["dsn"] == "postgresql://example/prahari"
    assert captured["min_size"] == 1
    assert captured["max_size"] == 4
    assert captured["init"] is _init_connection
    # A pod that cannot reach Postgres must fail readiness, not hang holding
    # a connection attempt open.
    assert captured["command_timeout"] == 30.0


# --- the migration runner --------------------------------------------------------


async def test_apply_migrations_runs_each_file_once_in_order(tmp_path):
    _write_migrations(
        tmp_path,
        {
            "001_first.sql": "CREATE TABLE t1 (id int)",
            "002_second.sql": "CREATE TABLE t2 (id int)",
        },
    )
    conn = FakeConn()

    applied = await apply_migrations(FakePool(conn), directory=tmp_path)
    assert applied == ["001_first", "002_second"]

    sql_ran = [s for s, _ in conn.statements if s.startswith("CREATE TABLE t")]
    assert sql_ran == ["CREATE TABLE t1 (id int)", "CREATE TABLE t2 (id int)"]

    # Second run: everything already applied at the recorded checksum.
    assert await apply_migrations(FakePool(conn), directory=tmp_path) == []
    assert conn.unlocked  # the advisory lock is always released


async def test_apply_migrations_rejects_an_edited_applied_file(tmp_path):
    """The failure this exists for: a laptop and the cloud on different
    schemas while both report success. Edit a new migration instead."""
    _write_migrations(tmp_path, {"001_first.sql": "CREATE TABLE t1 (id int)"})
    conn = FakeConn()
    await apply_migrations(FakePool(conn), directory=tmp_path)

    (tmp_path / "001_first.sql").write_text("CREATE TABLE t1 (id int, x int)")
    with pytest.raises(RuntimeError, match="has changed since it was applied"):
        await apply_migrations(FakePool(conn), directory=tmp_path)
    assert conn.unlocked  # released even when a checksum mismatch raises


async def test_apply_migrations_records_the_file_checksum(tmp_path):
    sql = "SELECT 'pin me'"
    _write_migrations(tmp_path, {"001_a.sql": sql})
    conn = FakeConn()
    await apply_migrations(FakePool(conn), directory=tmp_path)
    assert conn.applied["001_a"] == hashlib.sha256(sql.encode()).hexdigest()


# --- timescale probe --------------------------------------------------------------


class _FetchvalPool:
    def __init__(self, value) -> None:
        self.value = value

    async def fetchval(self, sql, *args):
        return self.value


async def test_timescale_available_reports_the_extension_probe():
    """Reported rather than assumed: on `postgis/postgis` the hypertable is a
    plain table and retention quietly falls back to the in-process pruner."""
    assert await timescale_available(_FetchvalPool(True)) is True
    assert await timescale_available(_FetchvalPool(False)) is False
