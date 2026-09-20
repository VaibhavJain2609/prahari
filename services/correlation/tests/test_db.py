"""db.py: the migration runner and PostgresSightings.

No real Postgres — same posture as the registry's suite (which tests
repositories without a database). `apply_migrations` is exercised against a
recording fake connection/pool; `PostgresSightings` against a fake pool whose
`fetch`/`execute` capture SQL and args. What IS tested for real is the part
that can silently drift: which SQL/args are emitted, and that a stored
`detection_pb` bytea decodes back into the exact `VehicleDetection`.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from prahari.v1 import common_pb2, events_pb2

from prahari_correlation.db import PostgresSightings, apply_migrations
from prahari_correlation.registry_client import DarkZone, GeoPoint
from prahari_correlation.routes import LinkKind, build_route
from prahari_correlation.store import plate_key


def _detection(
    camera_id: str,
    *,
    detection_id: str = "D1",
    plate_text: str | None = "GJ01AB1234",
    wall_clock_s_value: float = 1000.0,
) -> events_pb2.VehicleDetection:
    ts = Timestamp()
    ts.FromDatetime(datetime.fromtimestamp(wall_clock_s_value, tz=UTC))
    kwargs = {}
    if plate_text is not None:
        kwargs["plate"] = events_pb2.PlateReading(
            raw_text="GJ 01 AB 1234",
            normalised_text=plate_text,
            char_confidence=[0.9, 0.9, 0.8, 0.7],
        )
    return events_pb2.VehicleDetection(
        detection_id=detection_id,
        camera_id=camera_id,
        observed_at=common_pb2.StreamTime(wall_clock=ts),
        evidence_ref=f"evidence/{detection_id}",
        **kwargs,
    )


# --- fakes -------------------------------------------------------------------


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    """The slice of asyncpg.Connection `apply_migrations` uses."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.applied: dict[str, str] = {}  # version -> checksum, the ledger

    async def execute(self, sql: str, *args) -> None:
        self.executed.append((sql, args))
        if sql.startswith("INSERT INTO schema_migration"):
            self.applied[args[0]] = args[1]

    async def fetchval(self, sql: str, *args):
        return self.applied.get(args[0])

    def transaction(self):
        return _Tx()


class _FakePool:
    """`acquire()` for the migration runner plus `execute`/`fetch`/`fetchval`
    for PostgresSightings — the two never share a call in these tests."""

    def __init__(self, conn: _FakeConn | None = None, fetch_rows: list | None = None) -> None:
        self._conn = conn or _FakeConn()
        self.fetch_rows = fetch_rows or []
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.execute_calls: list[tuple[str, tuple]] = []

    def acquire(self):
        return _Acquire(self._conn)

    async def execute(self, sql: str, *args) -> None:
        self.execute_calls.append((sql, args))

    async def fetch(self, sql: str, *args) -> list:
        self.fetch_calls.append((sql, args))
        return self.fetch_rows

    async def fetchval(self, sql: str, *args):
        return 1


# --- migration runner ---------------------------------------------------------


def _write_migrations(tmp_path: Path) -> None:
    (tmp_path / "001_first.sql").write_text("CREATE TABLE one (id int);")
    (tmp_path / "002_second.sql").write_text("CREATE TABLE two (id int);")


async def test_migrations_apply_in_order_then_are_idempotent(tmp_path) -> None:
    _write_migrations(tmp_path)
    conn = _FakeConn()
    pool = _FakePool(conn)

    assert await apply_migrations(pool, directory=tmp_path) == [
        "001_first",
        "002_second",
    ]
    # The ledger rows were written with the files' real checksums.
    for name in ("001_first", "002_second"):
        expected = hashlib.sha256((tmp_path / f"{name}.sql").read_bytes()).hexdigest()
        assert conn.applied[name] == expected

    # Second run: everything already applied -> nothing ran again.
    assert await apply_migrations(pool, directory=tmp_path) == []


async def test_a_changed_applied_migration_is_rejected(tmp_path) -> None:
    _write_migrations(tmp_path)
    conn = _FakeConn()
    conn.applied["001_first"] = "0" * 64  # recorded checksum != file checksum

    with pytest.raises(RuntimeError, match="001_first has changed"):
        await apply_migrations(_FakePool(conn), directory=tmp_path)


# --- PostgresSightings --------------------------------------------------------


async def test_insert_detection_writes_queryable_columns_and_the_blob() -> None:
    pool = _FakePool()
    sightings = PostgresSightings(pool)  # type: ignore[arg-type] -- fake pool
    detection = _detection("CAM-A")

    await sightings.insert_detection(detection, "1700000000000-0")

    sql, args = pool.execute_calls[0]
    assert "INSERT INTO correlation_sightings" in sql
    assert "ON CONFLICT (detection_id) DO NOTHING" in sql
    (
        detection_id,
        camera_id,
        skeleton,
        normalised,
        char_confs,
        observed_at,
        stream_entry_id,
        blob,
    ) = args
    assert detection_id == "D1"
    assert camera_id == "CAM-A"
    # Indexed by the confusion skeleton — same key the in-memory store uses.
    assert skeleton == plate_key("GJ01AB1234")
    assert normalised == "GJ01AB1234"
    # float32 precision — compare against the proto's own repeated field,
    # not the literal the test constructed it with.
    assert char_confs == list(detection.plate.char_confidence)
    assert observed_at == datetime.fromtimestamp(1000.0, tz=UTC)
    assert stream_entry_id == "1700000000000-0"
    # The blob round-trips losslessly — bridging needs appearance_embedding
    # and hops need pts_ms/evidence_ref, none of which have columns.
    assert events_pb2.VehicleDetection.FromString(bytes(blob)) == detection


async def test_insert_detection_without_plate_or_id_uses_fallback_key() -> None:
    pool = _FakePool()
    sightings = PostgresSightings(pool)  # type: ignore[arg-type]
    detection = _detection("CAM-A", detection_id="", plate_text=None)

    await sightings.insert_detection(detection, "2-0")

    _sql, args = pool.execute_calls[0]
    assert args[0].startswith("fallback:CAM-A:")  # the store's dedup key shape
    assert args[2] is None and args[3] is None and args[4] is None


async def test_sightings_for_plate_queries_the_skeleton_and_reorders_asc() -> None:
    newer = _detection("CAM-B", detection_id="D2", wall_clock_s_value=1600.0)
    older = _detection("CAM-A", detection_id="D1", wall_clock_s_value=1000.0)
    # SQL returns DESC (most recent first, so LIMIT cuts the OLD end).
    pool = _FakePool(
        fetch_rows=[
            {"detection_pb": newer.SerializeToString()},
            {"detection_pb": older.SerializeToString()},
        ]
    )
    sightings = PostgresSightings(pool)  # type: ignore[arg-type]

    result = await sightings.sightings_for_plate("GJ 01 AB 1234", since_s=500.0, limit=100)

    sql, args = pool.fetch_calls[0]
    assert "plate_skeleton = $1" in sql and "observed_at >=" in sql
    assert "ORDER BY observed_at DESC" in sql
    assert args[0] == plate_key("GJ01AB1234")  # skeleton of the QUERY text
    assert args[1] == datetime.fromtimestamp(500.0, tz=UTC)
    assert args[2] == 100
    # ...but callers see oldest-first, matching DetectionStore.by_plate.
    assert [d.detection_id for d in result] == ["D1", "D2"]


async def test_unplated_in_range_queries_the_null_skeleton_pool() -> None:
    unplated = _detection("CAM-A", detection_id="DU", plate_text=None)
    pool = _FakePool(fetch_rows=[{"detection_pb": unplated.SerializeToString()}])
    sightings = PostgresSightings(pool)  # type: ignore[arg-type]

    result = await sightings.unplated_in_range(900.0, 1100.0, limit=50)

    sql, args = pool.fetch_calls[0]
    assert "plate_skeleton IS NULL" in sql and "BETWEEN $1 AND $2" in sql
    assert args == (
        datetime.fromtimestamp(900.0, tz=UTC),
        datetime.fromtimestamp(1100.0, tz=UTC),
        50,
    )
    assert [d.detection_id for d in result] == ["DU"]


async def test_build_route_reads_persisted_rows_end_to_end() -> None:
    """The point of the whole change: rows written before a restart still
    assemble into a route. Exercises PostgresSightings -> build_route with
    serialized blobs, i.e. exactly what the post-restart query path sees."""
    cam_a = _detection("CAM-A", detection_id="D1", wall_clock_s_value=1000.0)
    cam_c = _detection("CAM-C", detection_id="D2", wall_clock_s_value=1660.0)
    # Rows come back DESC from the query; the source reverses them.
    pool = _FakePool(
        fetch_rows=[
            {"detection_pb": cam_c.SerializeToString()},
            {"detection_pb": cam_a.SerializeToString()},
        ]
    )
    sightings = PostgresSightings(pool)  # type: ignore[arg-type]

    class _Registry:
        async def camera_location(self, camera_id):  # noqa: ANN001, ANN202
            return {"CAM-A": GeoPoint(0.0, 0.0), "CAM-C": GeoPoint(0.0, 0.10)}[camera_id]

        async def dark_zones(self):  # noqa: ANN202
            return [DarkZone(camera_id="CAM-DOWN", location=None)]

    result = await build_route("GJ01AB1234", sightings, _Registry(), 120.0, 0.85)

    assert [h.camera_id for h in result.hops] == ["CAM-A", "CAM-C"]
    assert result.hops[1].link_kind == LinkKind.PLATE
    assert result.rejected == []
