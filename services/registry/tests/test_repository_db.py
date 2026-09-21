"""The repository's SQL methods, driven against a scripted asyncpg-pool fake.

`test_repository.py` covers the pure helpers; this file covers the methods
that speak SQL. What is being tested is the code around the statements —
clause assembly, parameter order, org resolution, credential handling,
row→model mapping — which is everything this module does other than Postgres
itself. The fake records `(method, sql, args)` for every call and serves
canned rows from per-method queues, so each test can assert on both sides of
the boundary: what the repository asked, and what it did with the answer.

No test in this service opens a real database — that is the convention, and
it is why these tests run on every machine every time.
"""

from __future__ import annotations

import base64
import secrets
from collections import deque
from datetime import UTC, datetime, timedelta

import pytest

from prahari_registry.config import RegistrySettings
from prahari_registry.crypto import CredentialKeyError, encrypt_credential
from prahari_registry.health import HealthVerdict
from prahari_registry.models import (
    CameraCreate,
    CameraUpdate,
    GeoPoint,
    HealthState,
    Heartbeat,
    Lifecycle,
    OrgCreate,
    OrgKind,
    SyncResult,
)
from prahari_registry.repository import (
    CameraRepository,
    OrgRepository,
    WorkerRepository,
    WorkerSecretError,
    _point,
    hash_worker_secret,
)

CAM_ID = "00000000-0000-0000-0000-0000000000ab"
ORG_ID = "00000000-0000-0000-0000-000000000001"


def _key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def _row(**overrides) -> dict:
    """A `camera_current`-shaped record — a dict stands in for asyncpg.Record
    (`camera_from_row` only ever does `row["name"]` lookups)."""
    row = {
        "id": CAM_ID,
        "source": "gujarat-sentinel",
        "external_id": "cam-42",
        "latitude": 23.0,
        "longitude": 72.5,
        "site_name": "Zone 4 junction",
        "district": "Ahmedabad",
        "department": None,
        "owner": None,
        "org_id": ORG_ID,
        "adapter": "gateway",
        "camera_type": "ip",
        "vendor": None,
        "vms_platform": None,
        "codec": "h264",
        "native_width": 1920,
        "native_height": 1080,
        "rtsp_url": "rtsp://gateway.gov.example:8554/stream/42",
        "hls_url": None,
        "whep_url": None,
        "storage_location": None,
        "retention_days": None,
        "commissioned_at": None,
        "amc_expires_at": None,
        "lifecycle": "active",
        "catalogue_live": True,
        "present_in_catalogue": True,
        "last_seen_in_catalogue": None,
        "effective_health_state": "healthy",
        "effective_health_reason": None,
        "last_heartbeat_at": None,
        "last_frame_at": None,
        "observed_fps": 8.0,
        "declared_fps": 25.0,
        "black_frame_ratio": None,
        "tamper_suspected": False,
        "consecutive_failures": 0,
        "loop_epoch": 0,
        "last_error": None,
        "created_at": None,
        "updated_at": None,
    }
    row.update(overrides)
    return row


def _org_row(**overrides) -> dict:
    row = {
        "id": ORG_ID,
        "parent_id": None,
        "path": "gj",
        "kind": "state",
        "name": "Gujarat",
        "created_at": None,
    }
    row.update(overrides)
    return row


class FakeConn:
    """A pooled connection: delegates to the pool's queues so one `calls`
    log captures the whole conversation, transaction or not."""

    def __init__(self, pool: FakePool) -> None:
        self._pool = pool

    async def fetchrow(self, sql, *args):
        return await self._pool.fetchrow(sql, *args)

    async def fetch(self, sql, *args):
        return await self._pool.fetch(sql, *args)

    async def fetchval(self, sql, *args):
        return await self._pool.fetchval(sql, *args)

    async def execute(self, sql, *args):
        return await self._pool.execute(sql, *args)

    def transaction(self):
        return _Tx()


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Acquire:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    """Scripted `asyncpg.Pool`: per-method result queues, one shared call log."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple]] = []
        self.fetchrow_results: deque = deque()
        self.fetch_results: deque = deque()
        self.fetchval_results: deque = deque()
        self.execute_results: deque = deque()

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        return self.fetchrow_results.popleft() if self.fetchrow_results else None

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self.fetch_results.popleft() if self.fetch_results else []

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        return self.fetchval_results.popleft() if self.fetchval_results else None

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return self.execute_results.popleft() if self.execute_results else "OK"

    def acquire(self):
        return _Acquire(FakeConn(self))

    # Convenience for assertions: the SQL of the nth recorded call.
    def sql(self, n: int) -> str:
        return self.calls[n][1]

    def args(self, n: int) -> tuple:
        return self.calls[n][2]


def _repo(pool: FakePool, **settings) -> CameraRepository:
    return CameraRepository(pool, RegistrySettings(sync_enabled=False, **settings))


# --- _point ---------------------------------------------------------------------


def test_point_emits_wkt_longitude_first():
    """Longitude first — the commonest way to silently put every camera in
    the wrong hemisphere."""
    assert _point(GeoPoint(latitude=23.0, longitude=72.5)) == "SRID=4326;POINT(72.5 23.0)"
    assert _point(None) is None


# --- reads -----------------------------------------------------------------------


async def test_get_returns_the_camera_for_a_row():
    pool = FakePool()
    pool.fetchrow_results.append(_row())
    camera = await _repo(pool).get(CAM_ID, scope="gj")
    assert camera is not None and camera.id == CAM_ID
    assert camera.health.state == HealthState.HEALTHY
    assert pool.args(0) == (CAM_ID, "gj")


async def test_get_returns_none_when_out_of_scope_or_absent():
    assert await _repo(FakePool()).get(CAM_ID, scope="gj") is None


async def test_get_by_external_looks_up_the_source_namespaced_id():
    pool = FakePool()
    pool.fetchrow_results.append(_row())
    camera = await _repo(pool).get_by_external("manual", "cam-x", scope="gj")
    assert camera is not None
    assert pool.args(0) == ("manual", "cam-x", "gj")
    assert await _repo(FakePool()).get_by_external("manual", "nope", scope="gj") is None


async def test_list_with_no_filters_queries_scope_and_default_lifecycle():
    pool = FakePool()
    pool.fetch_results.append([_row()])
    cameras = await _repo(pool).list(scope="gj")
    assert [c.id for c in cameras] == [CAM_ID]
    sql = pool.sql(0)
    assert "o.path <@ $1::ltree" in sql
    assert "lifecycle = $2" in sql  # lifecycle defaults to active
    assert pool.args(0) == ("gj", "active", 500, 0)


async def test_list_assembles_one_clause_per_supplied_filter():
    """The dynamic WHERE clause is the logic under test: every supplied
    filter must add exactly one parameterised clause, in a fixed order."""
    pool = FakePool()
    pool.fetch_results.append([])
    await _repo(pool).list(
        scope="gj",
        district="Ahmedabad",
        department="Traffic",
        state=HealthState.DEGRADED,
        lifecycle=Lifecycle.ABSENT,
        search="junction",
        bbox=(72.0, 23.0, 73.0, 24.0),
        limit=50,
        offset=10,
    )
    sql, args = pool.sql(0), pool.args(0)
    assert "district = $3" in sql
    assert "department = $4" in sql
    assert "effective_health_state = $5" in sql
    assert "ILIKE $6" in sql
    assert "ST_MakeEnvelope($7, $8, $9, $10, 4326)" in sql
    assert args == (
        "gj",
        "absent",
        "Ahmedabad",
        "Traffic",
        "degraded",
        "%junction%",
        72.0,
        23.0,
        73.0,
        24.0,
        50,
        10,
    )


async def test_list_without_lifecycle_filter_omits_the_clause():
    pool = FakePool()
    pool.fetch_results.append([])
    await _repo(pool).list(scope="gj", lifecycle=None)
    sql = pool.sql(0)
    assert "lifecycle" not in sql
    assert pool.args(0) == ("gj", 500, 0)


async def test_count_filters_by_lifecycle_only_when_one_is_given():
    pool = FakePool()
    pool.fetchval_results.extend([7, 12])
    repo = _repo(pool)
    assert await repo.count(scope="gj", lifecycle=None) == 7
    assert "lifecycle" not in pool.sql(0)
    assert await repo.count(scope="gj") == 12
    assert pool.args(1) == ("gj", "active")


async def test_health_summary_reports_every_state_even_when_db_returns_fewer():
    """The summary is keyed on every HealthState so a console widget can rely
    on the keys existing; the DB only returns states that occurred."""
    pool = FakePool()
    pool.fetch_results.append([{"state": "healthy", "n": 3}])
    summary = await _repo(pool).health_summary(scope="gj")
    assert summary == {s.value: 0 for s in HealthState} | {"healthy": 3}


# --- org resolution -----------------------------------------------------------------


async def test_org_id_for_path_and_org_path_for_id():
    pool = FakePool()
    pool.fetchval_results.extend([ORG_ID, "gj.ahmedabad"])
    repo = _repo(pool)
    assert await repo.org_id_for_path("gj") == ORG_ID
    assert await repo.org_path_for_id(ORG_ID) == "gj.ahmedabad"
    pool.fetchval_results.append(None)
    assert await repo.org_id_for_path("nowhere") is None


async def test_resolve_org_raises_for_an_unknown_org_id():
    """A camera that fails to get an org must fail to be created, not land
    invisible to every scoped read."""
    repo = _repo(FakePool())  # fetchval queue empty → no org row
    with pytest.raises(ValueError, match="no org"):
        await repo._resolve_org("00000000-0000-0000-0000-000000000099")


async def test_resolve_org_raises_when_the_default_path_is_unseeded():
    repo = _repo(FakePool())
    with pytest.raises(ValueError, match="sync_default_org_path"):
        await repo._resolve_org(None)


# --- writes -------------------------------------------------------------------------


async def test_create_with_explicit_org_inserts_and_rereads():
    pool = FakePool()
    # fetchval: org_path_for_id; fetchrow: INSERT RETURNING id, then get().
    pool.fetchval_results.append("gj.ahmedabad")
    pool.fetchrow_results.extend([{"id": CAM_ID}, _row()])
    repo = _repo(pool)

    payload = CameraCreate(external_id="cam-x", org_id=ORG_ID, site_name="Depot")
    camera = await repo.create(payload)

    assert camera.id == CAM_ID
    insert_sql, insert_args = pool.sql(1), pool.args(1)
    assert "INSERT INTO cameras" in insert_sql
    assert insert_args[0:2] == ("manual", "cam-x")
    assert insert_args[7] == ORG_ID  # org_id bound, not the default
    # The re-read is scoped to the org the camera landed in.
    assert pool.args(2) == (CAM_ID, "gj.ahmedabad")


async def test_create_without_org_resolves_the_default_path():
    pool = FakePool()
    pool.fetchval_results.append(ORG_ID)  # org_id_for_path("gj")
    pool.fetchrow_results.extend([{"id": CAM_ID}, _row()])
    camera = await _repo(pool).create(CameraCreate(external_id="cam-x"))
    assert camera.id == CAM_ID
    assert pool.args(0) == ("gj",)
    assert pool.args(1)[7] == ORG_ID


async def test_create_encrypts_a_supplied_stream_password():
    """`stream_password` must land as `stream_secret` ciphertext — there is no
    code path that writes a plaintext credential column."""
    pool = FakePool()
    pool.fetchval_results.append(ORG_ID)
    pool.fetchrow_results.extend([{"id": CAM_ID}, _row()])
    repo = _repo(pool, credential_key=_key())
    await repo.create(CameraCreate(external_id="dvr-1", stream_password="factory-pw"))
    secret = pool.args(1)[10]  # stream_secret parameter
    assert isinstance(secret, bytes) and b"factory-pw" not in secret


async def test_create_with_a_password_and_no_key_fails_closed():
    pool = FakePool()
    pool.fetchval_results.append(ORG_ID)
    with pytest.raises(CredentialKeyError):
        await _repo(pool).create(CameraCreate(external_id="dvr-1", stream_password="x"))
    assert not any("INSERT" in sql for _, sql, _ in pool.calls)


async def test_update_builds_a_set_clause_only_for_sent_fields():
    pool = FakePool()
    pool.fetchrow_results.extend([{"id": CAM_ID}, _row(district="Surat")])
    repo = _repo(pool)
    camera = await repo.update(
        CAM_ID,
        CameraUpdate(district="Surat", camera_type="ptz", stale_after_s=90),
        scope="gj",
    )
    assert camera is not None and camera.district == "Surat"
    sql, args = pool.sql(0), pool.args(0)
    assert "district = $1" in sql
    assert "camera_type = $2" in sql
    assert "stale_after_s = $3" in sql
    # id and scope are the final two parameters of the WHERE clause.
    assert args[-2:] == (CAM_ID, "gj")


async def test_update_location_goes_through_the_geography_cast():
    pool = FakePool()
    pool.fetchrow_results.extend([{"id": CAM_ID}, _row()])
    await _repo(pool).update(
        CAM_ID, CameraUpdate(location=GeoPoint(latitude=23.0, longitude=72.5)), scope="gj"
    )
    assert "location = $1::geography" in pool.sql(0)
    assert pool.args(0)[0] == "SRID=4326;POINT(72.5 23.0)"


async def test_update_stream_password_writes_the_encrypted_column():
    """The plaintext field name deliberately differs from the column so only
    this branch can write it — the SQL must set `stream_secret`, never
    `stream_password`."""
    pool = FakePool()
    pool.fetchrow_results.extend([{"id": CAM_ID}, _row()])
    repo = _repo(pool, credential_key=_key())
    await repo.update(CAM_ID, CameraUpdate(stream_password="new-pw"), scope="gj")
    sql = pool.sql(0)
    assert "stream_secret = $1" in sql
    assert "stream_password" not in sql


async def test_update_with_no_fields_is_a_plain_read():
    pool = FakePool()
    pool.fetchrow_results.append(_row())
    camera = await _repo(pool).update(CAM_ID, CameraUpdate(), scope="gj")
    assert camera is not None
    assert len(pool.calls) == 1  # only the SELECT, no UPDATE


async def test_update_out_of_scope_returns_none():
    pool = FakePool()
    pool.fetchrow_results.append(None)  # UPDATE ... RETURNING matched nothing
    assert await _repo(pool).update(CAM_ID, CameraUpdate(district="X"), scope="gj") is None


async def test_decommission_is_a_soft_delete():
    pool = FakePool()
    pool.fetchrow_results.extend([{"id": CAM_ID}, _row(lifecycle="decommissioned")])
    camera = await _repo(pool).decommission(CAM_ID, scope="gj")
    assert camera is not None and camera.lifecycle == Lifecycle.DECOMMISSIONED
    sql = pool.sql(0)
    assert "lifecycle = 'decommissioned'" in sql and "DELETE" not in sql


async def test_decommission_out_of_scope_returns_none():
    assert await _repo(FakePool()).decommission(CAM_ID, scope="gj") is None


# --- catalogue upsert / absence ------------------------------------------------------


async def test_upsert_from_catalogue_returns_id_and_inserted_flag():
    pool = FakePool()
    pool.fetchrow_results.extend(
        [{"id": CAM_ID, "inserted": True}, {"id": CAM_ID, "inserted": False}]
    )
    repo = _repo(pool)
    conn = FakeConn(pool)
    kwargs = dict(
        source="gw",
        external_id="cam-1",
        site_name="Junction",
        location=GeoPoint(latitude=23.0, longitude=72.5),
        codec="h264",
        native_width=1920,
        native_height=1080,
        declared_fps=25.0,
        rtsp_url="rtsp://gw/1",
        hls_url=None,
        whep_url=None,
        catalogue_live=True,
        raw={"id": "cam-1"},
        seen_at=datetime.now(UTC),
        default_org_id=ORG_ID,
    )
    assert await repo.upsert_from_catalogue(conn, **kwargs) == (CAM_ID, True)
    assert await repo.upsert_from_catalogue(conn, **kwargs) == (CAM_ID, False)
    sql = pool.sql(0)
    # org_id is written on INSERT only — a camera a local body has claimed is
    # never moved back by a later sync.
    assert "ON CONFLICT" in sql and "org_id = EXCLUDED" not in sql


async def test_mark_absent_flags_unseen_cameras_and_returns_the_count():
    pool = FakePool()
    pool.fetchval_results.append(4)
    n = await _repo(pool).mark_absent(FakeConn(pool), source="gw", seen_ids=[CAM_ID])
    assert n == 4
    assert pool.args(0) == ("gw", [CAM_ID])
    sql = pool.sql(0)
    assert "lifecycle = 'absent'" in sql and "DELETE" not in sql


# --- health -------------------------------------------------------------------------


async def test_recent_health_history_splits_fps_from_tamper_and_skips_nulls():
    pool = FakePool()
    pool.fetch_results.append(
        [
            {"measured_fps": 8.0, "tamper_suspected": False},
            {"measured_fps": None, "tamper_suspected": True},
        ]
    )
    fps, tamper = await _repo(pool).recent_health_history(CAM_ID, window_s=3600, limit=60)
    assert fps == [8.0]  # a null measured_fps is not a zero
    assert tamper == [False, True]
    assert pool.args(0) == (CAM_ID, 3600, 60)


async def test_health_history_maps_rows_to_samples():
    pool = FakePool()
    observed = datetime.now(UTC)
    pool.fetch_results.append(
        [
            {
                "observed_at": observed,
                "worker_id": "w1",
                "connected": True,
                "measured_fps": 7.5,
                "last_frame_at": None,
                "frames_decoded": 100,
                "consecutive_failures": 0,
                "black_frame_ratio": None,
                "tamper_suspected": False,
                "loop_epoch": 0,
                "last_error": None,
            }
        ]
    )
    since = observed - timedelta(hours=1)
    samples = await _repo(pool).health_history(CAM_ID, scope="gj", since=since, limit=25)
    assert len(samples) == 1
    assert samples[0].worker_id == "w1" and samples[0].measured_fps == 7.5
    assert pool.args(0) == (CAM_ID, "gj", since, 25)


async def test_prune_heartbeats_parses_the_delete_count_from_the_command_tag():
    pool = FakePool()
    pool.execute_results.extend(["DELETE 1204", "DELETE 0"])
    repo = _repo(pool)
    assert await repo.prune_heartbeats(retention_days=14) == 1204
    assert await repo.prune_heartbeats(retention_days=14) == 0


async def test_record_heartbeat_writes_the_row_and_the_denormalised_cache():
    pool = FakePool()
    repo = _repo(pool)
    future = datetime.now(UTC) + timedelta(days=1)  # must be clamped, not stored
    heartbeat = Heartbeat(
        worker_id="w1",
        observed_at=future,
        connected=True,
        measured_fps=8.0,
        consecutive_failures=0,
    )
    verdict = HealthVerdict(state=HealthState.HEALTHY, reason="ok", baseline_fps=8.0)
    await repo.record_heartbeat(CAM_ID, heartbeat, verdict)

    insert = next(c for c in pool.calls if "INSERT INTO camera_heartbeat" in c[1])
    update = next(c for c in pool.calls if "UPDATE cameras" in c[1])
    stored_at = insert[2][1]
    # The clamped timestamp is what lands in the table — a worker clock a day
    # ahead must not pin last_heartbeat_at forward via GREATEST.
    assert stored_at <= datetime.now(UTC)
    assert "GREATEST" in update[1]
    assert update[2][1] == "healthy"  # the verdict's state, not the reporter's claim


# --- sync bookkeeping ------------------------------------------------------------------


async def test_sync_run_bookkeeping_round_trips_through_the_model():
    pool = FakePool()
    pool.fetchval_results.append(7)
    repo = _repo(pool)
    run_id = await repo.start_sync_run("gw")
    assert run_id == 7
    assert pool.args(0) == ("gw",)

    result = SyncResult(
        source="gw",
        ok=True,
        started_at=datetime.now(UTC),
        cameras_seen=3,
        codec_mix={"h264": 3},
    )
    await repo.finish_sync_run(run_id, result)
    update_args = next(c for c in pool.calls if "UPDATE catalogue_sync_run" in c[1])[2]
    assert update_args[0] == 7 and update_args[1] is True

    pool.fetch_results.append(
        [
            {
                "source": "gw",
                "ok": True,
                "started_at": result.started_at,
                "finished_at": None,
                "cameras_seen": 3,
                "cameras_added": 1,
                "cameras_updated": 2,
                "cameras_absent": 0,
                "codec_mix": {"h264": 3},
                "error": None,
            }
        ]
    )
    runs = await repo.last_sync_runs(5)
    assert len(runs) == 1 and runs[0].codec_mix == {"h264": 3}
    assert pool.calls[-1][2] == (5,)


# --- fan-out -------------------------------------------------------------------------


async def test_desired_mediamtx_paths_injects_decrypted_credentials():
    """The one place the raw upstream URL is assembled: the path value must
    carry `user:pass@` for MediaMTX's pull — and this is exactly why the
    mapping may never leave the process in an HTTP response."""
    key = _key()
    secret = encrypt_credential("dvr-pw", key)
    pool = FakePool()
    pool.fetch_results.append(
        [
            {
                "id": "1",
                "rtsp_url": "rtsp://10.0.0.5:554/ch1",
                "stream_username": "admin",
                "stream_secret": secret,
            },
            {
                "id": "2",
                "rtsp_url": "rtsp://10.0.0.6:554/ch2",
                "stream_username": None,
                "stream_secret": None,
            },
        ]
    )
    paths = await _repo(pool, credential_key=key).desired_mediamtx_paths()
    assert paths["cam-1"] == "rtsp://admin:dvr-pw@10.0.0.5:554/ch1"
    assert paths["cam-2"] == "rtsp://10.0.0.6:554/ch2"


async def test_desired_mediamtx_paths_uses_a_blank_username_when_none_stored():
    key = _key()
    pool = FakePool()
    pool.fetch_results.append(
        [
            {
                "id": "1",
                "rtsp_url": "rtsp://dvr/live",
                "stream_username": None,
                "stream_secret": encrypt_credential("pw", key),
            }
        ]
    )
    assert (await _repo(pool, credential_key=key).desired_mediamtx_paths())[
        "cam-1"
    ] == "rtsp://:pw@dvr/live"


# --- workers --------------------------------------------------------------------------


async def test_register_refreshes_the_lease_and_persists_the_coordinates():
    pool = FakePool()
    pool.fetch_results.append([{"worker_id": "w1"}, {"worker_id": "w2"}])
    repo = WorkerRepository(pool, RegistrySettings(sync_enabled=False, assignment_lease_s=60))

    registration = await repo.register("w2")
    assert (registration.shard_index, registration.shard_count) == (1, 2)
    assert registration.lease_s == 60
    assert registration.worker_secret is None  # no flag, no mint
    statements = [
        sql for _, sql, _ in pool.calls if "INSERT INTO workers" in sql or "UPDATE workers" in sql
    ]
    assert any("ON CONFLICT" in s for s in statements)
    assert any("shard_index" in s for s in statements)


async def test_register_mints_a_secret_only_on_the_opt_in_flag():
    """rotate=true on an UNBOUND worker_id is the bind request: the plaintext
    is returned once and the upsert stores only its digest. The default path
    must never mint — a secret the caller did not ask for is a credential it
    cannot present on the next call."""
    pool = FakePool()  # fetchval -> None: no stored hash
    pool.fetch_results.append([{"worker_id": "w1"}])
    repo = WorkerRepository(pool, RegistrySettings(sync_enabled=False, assignment_lease_s=60))

    registration = await repo.register("w1", rotate=True)
    assert registration.worker_secret is not None
    insert_args = next(args for _, sql, args in pool.calls if "INSERT INTO workers" in sql)
    # $2 is the stored digest — never the plaintext itself.
    assert insert_args == ("w1", hash_worker_secret(registration.worker_secret))


async def test_register_bound_worker_requires_the_current_secret():
    pool = FakePool()
    stored = hash_worker_secret("s3kr3t")
    repo = WorkerRepository(pool, RegistrySettings(sync_enabled=False, assignment_lease_s=60))

    # Missing and wrong both refuse before the upsert — each attempt reads
    # the same stored digest.
    pool.fetchval_results.extend([stored, stored])
    with pytest.raises(WorkerSecretError):
        await repo.register("w1")
    with pytest.raises(WorkerSecretError):
        await repo.register("w1", presented_secret="wrong")
    assert not any("INSERT INTO workers" in sql for _, sql, _ in pool.calls)

    # The right secret refreshes the lease WITHOUT re-minting.
    pool.fetchval_results.append(stored)
    pool.fetch_results.append([{"worker_id": "w1"}])
    registration = await repo.register("w1", presented_secret="s3kr3t")
    assert registration.worker_secret is None
    insert_args = next(args for _, sql, args in pool.calls if "INSERT INTO workers" in sql)
    assert insert_args[1] is None  # COALESCE($2, ...) keeps the stored hash


async def test_register_rotate_replaces_the_hash_and_returns_the_new_secret():
    pool = FakePool()
    old_hash = hash_worker_secret("old-secret")
    repo = WorkerRepository(pool, RegistrySettings(sync_enabled=False, assignment_lease_s=60))

    # Rotation is gated on the CURRENT secret — a stolen one cannot re-key.
    pool.fetchval_results.append(old_hash)
    with pytest.raises(WorkerSecretError):
        await repo.register("w1", presented_secret="wrong", rotate=True)

    pool.fetchval_results.append(old_hash)
    pool.fetch_results.append([{"worker_id": "w1"}])
    registration = await repo.register("w1", presented_secret="old-secret", rotate=True)
    assert registration.worker_secret and registration.worker_secret != "old-secret"
    insert_args = next(args for _, sql, args in pool.calls if "INSERT INTO workers" in sql)
    assert insert_args[1] == hash_worker_secret(registration.worker_secret)


async def test_worker_secret_required_refuses_an_unbound_registration():
    """Armed, the flag closes the last unbound path: a register that does not
    ask for a secret is refused rather than left claimable by any token
    holder. The mint path itself still works — that is the upgrade path."""
    repo = WorkerRepository(
        FakePool(),
        RegistrySettings(sync_enabled=False, assignment_lease_s=60, worker_secret_required=True),
    )
    pool = repo._pool
    with pytest.raises(WorkerSecretError):
        await repo.register("w1")  # fetchval -> None: unbound

    pool.fetch_results.append([{"worker_id": "w1"}])
    registration = await repo.register("w1", rotate=True)
    assert registration.worker_secret is not None


async def test_bound_secret_hash_returns_the_stored_digest_or_none():
    """None covers both "no row" and "unbound" — the callers only need to
    know whether a credential must be presented."""
    pool = FakePool()
    pool.fetchval_results.extend(["abc123", None])
    repo = WorkerRepository(pool, RegistrySettings(sync_enabled=False))
    assert await repo.bound_secret_hash("w1") == "abc123"
    assert await repo.bound_secret_hash("w2") is None


async def test_alive_worker_ids_uses_the_alive_horizon():
    pool = FakePool()
    pool.fetch_results.append([{"worker_id": "w1"}, {"worker_id": "w2"}])
    repo = WorkerRepository(pool, RegistrySettings(sync_enabled=False, assignment_lease_s=30))
    assert await repo.alive_worker_ids() == ["w1", "w2"]
    # The alive window is 2x the assignment lease — the pool math, not a guess.
    assert pool.args(0) == (60,)
    assert "ORDER BY worker_id" in pool.sql(0)


async def test_assignment_registers_then_slices_the_estate():
    pool = FakePool()
    pool.fetch_results.extend(
        [
            [{"worker_id": "w1"}],  # alive set during register
            [_row()],  # the shard's cameras
        ]
    )
    repo = WorkerRepository(pool, RegistrySettings(sync_enabled=False, assignment_lease_s=60))
    assignment = await repo.assignment("w1", scope="gj")
    assert assignment.worker_id == "w1" and assignment.shard_count == 1
    assert [c.id for c in assignment.cameras] == [CAM_ID]
    # The slice is modulo on row_number over ORDER BY id — recomputed, never stored.
    assert "row_number() OVER (ORDER BY cc.id)" in pool.sql(-1)


async def test_prune_stale_parses_the_delete_count():
    pool = FakePool()
    pool.execute_results.extend(["DELETE 3", "SELECT 1"])
    repo = WorkerRepository(pool, RegistrySettings(sync_enabled=False, assignment_lease_s=30))
    assert await repo.prune_stale() == 3
    assert pool.args(0) == (90,)  # 3x the lease
    assert await repo.prune_stale() == 0  # a non-DELETE tag means nothing reaped


# --- orgs ---------------------------------------------------------------------------


async def test_org_create_at_root_uses_the_label_as_path():
    pool = FakePool()
    pool.fetchrow_results.append(_org_row())
    org = await OrgRepository(pool).create(
        OrgCreate(label="gj", kind=OrgKind.STATE, name="Gujarat")
    )
    assert org.path == "gj"
    assert pool.args(0)[1] == "gj"


async def test_org_create_under_a_parent_extends_its_path():
    pool = FakePool()
    pool.fetchrow_results.extend(
        [
            _org_row(),  # the parent's row, from get()
            _org_row(id="child", parent_id=ORG_ID, path="gj.amd", name="Ahmedabad"),
        ]
    )
    repo = OrgRepository(pool)
    org = await repo.create(
        OrgCreate(parent_id=ORG_ID, label="amd", kind=OrgKind.LOCAL_BODY, name="Ahmedabad")
    )
    assert org.path == "gj.amd" and org.parent_id == ORG_ID
    assert pool.args(1)[1] == "gj.amd"


async def test_org_create_with_a_missing_parent_raises():
    """Enforced at the repository — the model cannot know whether the parent
    exists, and an org with no parent path would float outside every scope."""
    with pytest.raises(ValueError, match="no org"):
        await OrgRepository(FakePool()).create(
            OrgCreate(parent_id=ORG_ID, label="amd", kind=OrgKind.LOCAL_BODY, name="Amd")
        )


async def test_org_get_and_get_by_path():
    pool = FakePool()
    pool.fetchrow_results.extend([_org_row(), None, _org_row(path="gj.amd")])
    repo = OrgRepository(pool)
    assert (await repo.get(ORG_ID)).path == "gj"
    assert await repo.get("nope") is None
    assert (await repo.get_by_path("gj.amd")).id == ORG_ID


async def test_org_list_subtree_returns_every_org_at_or_below_scope():
    pool = FakePool()
    pool.fetch_results.append([_org_row(), _org_row(id="child", path="gj.amd", parent_id=ORG_ID)])
    orgs = await OrgRepository(pool).list_subtree("gj")
    assert [o.path for o in orgs] == ["gj", "gj.amd"]
    assert "path <@ $1::ltree" in pool.sql(0)
