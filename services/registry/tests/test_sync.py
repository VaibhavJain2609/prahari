"""Catalogue sync, exercised against a captured snapshot rather than the live
gateway — the whole point of `CatalogueClient.load_snapshot`.

The database calls are faked. What is being tested here is the sync's contract:
what it counts, what it marks absent, and that a gateway failure leaves the
registry intact rather than half-written.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from prahari_common.catalogue import Catalogue, CatalogueClient

from prahari_registry.config import RegistrySettings
from prahari_registry.models import SyncResult
from prahari_registry.sync import CatalogueSync

SNAPSHOT = {
    "fetched_at": "2026-09-01T10:00:00+00:00",
    "camera_count": 3,
    "live_count": 2,
    "codec_mix": {"h264": 2, "h265": 1},
    "cameras": [
        {
            "id": "101",
            "name": "Ashram Road Junction",
            "latitude": 23.0225,
            "longitude": 72.5714,
            "live": True,
            "codec": "H264",
            "resolution": "1920x1080",
            "fps": 25,
        },
        {
            "id": "102",
            "name": "SG Highway Toll",
            "latitude": 23.0400,
            "longitude": 72.5100,
            "live": True,
            "codec": "H265",
            "width": 2560,
            "height": 1440,
        },
        {"id": "103", "name": "Kalupur Station", "live": False, "codec": "H264"},
    ],
}


@pytest.fixture
def snapshot(tmp_path: Path) -> Catalogue:
    path = tmp_path / "ingest-20260901T100000Z.json"
    path.write_text(json.dumps(SNAPSHOT), encoding="utf-8")
    return CatalogueClient.load_snapshot(path)


class FakeRepo:
    """Records what sync asked for. Standing in for the database keeps these
    tests runnable with no container, which is what makes them get run."""

    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing = existing or set()
        self.upserts: list[dict] = []
        self.absent_call: dict | None = None
        self.runs: list[SyncResult] = []
        self.paths_asked = False

    async def start_sync_run(self, source: str) -> int:
        return 1

    async def org_id_for_path(self, path: str) -> str | None:
        """Stands in for the seeded `gj` root (migrations/005_orgs.sql). Every
        sync pass resolves this once before the per-camera loop; a repo with
        no orgs at all would make `run_once` raise, which is exactly what
        should happen against a real database missing its seed row."""
        return f"org-{path}"

    async def finish_sync_run(self, run_id: int, result: SyncResult) -> None:
        self.runs.append(result)

    async def upsert_from_catalogue(self, conn, **kwargs) -> tuple[str, bool]:
        self.upserts.append(kwargs)
        external_id = kwargs["external_id"]
        inserted = external_id not in self.existing
        self.existing.add(external_id)
        return f"uuid-{external_id}", inserted

    async def mark_absent(self, conn, *, source: str, seen_ids) -> int:
        self.absent_call = {"source": source, "seen_ids": list(seen_ids)}
        return 0

    async def desired_mediamtx_paths(self) -> dict[str, str]:
        self.paths_asked = True
        return {}


class FakeConn:
    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def acquire(self):
        return FakeConn()


class FakeMediaMTX:
    def __init__(self) -> None:
        self.reconciled: list[dict] = []

    async def reconcile(self, desired: dict[str, str]):
        self.reconciled.append(desired)
        return None

    async def aclose(self) -> None:
        return None


def make_sync(repo: FakeRepo, mediamtx: FakeMediaMTX | None = None) -> CatalogueSync:
    return CatalogueSync(
        pool=FakePool(),
        repo=repo,
        settings=RegistrySettings(catalogue_source="test-gateway"),
        gateway=None,
        mediamtx=mediamtx or FakeMediaMTX(),
    )


async def test_sync_ingests_every_camera_including_the_dead_one(snapshot: Catalogue):
    """A camera the catalogue reports as not live is still registered.

    It is part of the estate and part of the coverage gap. Skipping it would
    make the gap analysis quietly optimistic — the exact failure Model 1 exists
    to prevent.
    """
    repo = FakeRepo()
    result = await make_sync(repo).run_once(snapshot)

    assert result.ok
    assert result.cameras_seen == 3
    assert result.cameras_added == 3
    assert {u["external_id"] for u in repo.upserts} == {"101", "102", "103"}
    assert [u["catalogue_live"] for u in repo.upserts] == [True, True, False]


async def test_sync_is_idempotent(snapshot: Catalogue):
    """Second run adds nothing. It runs on startup, on a timer, and whenever the
    button is pressed during a demo."""
    repo = FakeRepo()
    sync = make_sync(repo)

    first = await sync.run_once(snapshot)
    second = await sync.run_once(snapshot)

    assert (first.cameras_added, first.cameras_updated) == (3, 0)
    assert (second.cameras_added, second.cameras_updated) == (0, 3)


async def test_sync_reports_the_codec_mix(snapshot: Catalogue):
    """Mixed H.264/H.265 is a stated requirement, so which mix we have actually
    run against is recorded per sync rather than asserted in a slide."""
    result = await make_sync(FakeRepo()).run_once(snapshot)
    assert result.codec_mix == {"h264": 2, "h265": 1}


async def test_sync_passes_every_seen_id_to_mark_absent(snapshot: Catalogue):
    repo = FakeRepo()
    await make_sync(repo).run_once(snapshot)
    assert repo.absent_call == {
        "source": "test-gateway",
        "seen_ids": ["uuid-101", "uuid-102", "uuid-103"],
    }


async def test_declared_fps_is_carried_but_stays_declared(snapshot: Catalogue):
    """The catalogue's 25 fps is recorded for drift reporting. Nothing derives
    time from it — the column it lands in is `declared_fps`, and health compares
    against measured history."""
    repo = FakeRepo()
    await make_sync(repo).run_once(snapshot)
    by_id = {u["external_id"]: u for u in repo.upserts}
    assert by_id["101"]["declared_fps"] == 25
    assert by_id["102"]["declared_fps"] is None


async def test_resolution_string_is_parsed_into_dimensions(snapshot: Catalogue):
    """Batching shape depends on real dimensions, and the catalogue supplies
    them in at least two shapes."""
    repo = FakeRepo()
    await make_sync(repo).run_once(snapshot)
    by_id = {u["external_id"]: u for u in repo.upserts}
    assert (by_id["101"]["native_width"], by_id["101"]["native_height"]) == (1920, 1080)
    assert (by_id["102"]["native_width"], by_id["102"]["native_height"]) == (2560, 1440)


async def test_synced_cameras_land_in_the_configured_default_org(snapshot: Catalogue):
    """Every camera this pass inserts carries the resolved default org id, so a
    fresh sync onto an empty registry does not leave `org_id` for the caller
    to guess at."""
    repo = FakeRepo()
    await make_sync(repo).run_once(snapshot)
    assert all(u["default_org_id"] == "org-gj" for u in repo.upserts)


async def test_missing_default_org_fails_the_run_without_raising(snapshot: Catalogue):
    """A registry whose seed row (migrations/005_orgs.sql) was somehow removed
    must fail the pass loudly rather than upsert cameras into nowhere."""

    class NoOrgRepo(FakeRepo):
        async def org_id_for_path(self, path: str) -> str | None:
            return None

    repo = NoOrgRepo()
    result = await make_sync(repo).run_once(snapshot)

    assert not result.ok
    assert "sync_default_org_path" in result.error
    assert repo.upserts == []


async def test_a_missing_gateway_fails_the_run_without_raising():
    """A gateway blip must not take the registry down. The estate it already
    knows about is still valid and health tracking must keep running."""
    repo = FakeRepo()
    result = await make_sync(repo).run_once()

    assert not result.ok
    assert "gateway credentials" in result.error
    assert repo.runs[-1].error == result.error


async def test_failed_sync_does_not_reconcile_mediamtx():
    """Reconciling from a half-read catalogue would delete paths for cameras
    that are still there."""
    mediamtx = FakeMediaMTX()
    await make_sync(FakeRepo(), mediamtx).run_once()
    assert mediamtx.reconciled == []


async def test_successful_sync_reconciles_mediamtx(snapshot: Catalogue):
    mediamtx = FakeMediaMTX()
    await make_sync(FakeRepo(), mediamtx).run_once(snapshot)
    assert len(mediamtx.reconciled) == 1


async def test_finish_sync_run_failure_does_not_mask_the_result(snapshot: Catalogue):
    """Recording the run is bookkeeping; the pass already happened. A failure
    there must not turn a good sync (or its real error) into an exception."""

    class BadFinishRepo(FakeRepo):
        async def finish_sync_run(self, run_id: int, result: SyncResult) -> None:
            raise RuntimeError("db gone mid-write")

    result = await make_sync(BadFinishRepo()).run_once(snapshot)
    assert result.ok
    assert result.finished_at is not None


async def test_sync_loop_survives_a_failing_first_pass():
    """The startup pass used to run outside the loop's try/except: one
    transient failure at boot — the likeliest time for a DB blip — killed the
    background task and silently ended every future sync."""
    sync = make_sync(FakeRepo())
    sync._s = RegistrySettings(
        catalogue_source="test-gateway", sync_on_startup=True, sync_interval_s=0.01
    )
    calls = 0

    async def flaky_pass():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("db blip at boot")
        return None

    sync.run_once_locked = flaky_pass  # type: ignore[method-assign]
    task = asyncio.create_task(sync._loop())
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The loop kept running past the raising first pass.
    assert calls > 1


def test_snapshot_round_trip_needs_no_network(snapshot: Catalogue):
    """Development against a captured snapshot is the only way to build this
    without holding connections open to a shared government feed."""
    assert len(snapshot.cameras) == 3
    assert len(snapshot.live_cameras) == 2
    assert snapshot.fetched_at == datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
