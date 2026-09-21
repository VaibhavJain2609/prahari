"""The rest of the HTTP surface: orgs, camera CRUD, readiness, catalogue sync,
stream fan-out, gap analysis, `/metrics`, the lifespan and the retention loop.

Same approach as test_app.py — `TestClient(app)` without the lifespan
context, fakes installed on `app.state` per fixture. The lifespan itself is
tested by monkeypatching its three real-Postgres touchpoints
(`create_pool`, `apply_migrations`, `timescale_available`); everything else it
wires is a plain object constructor.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import prahari_registry.app as registry_app
from prahari_registry.app import app
from prahari_registry.config import RegistrySettings
from prahari_registry.crypto import CredentialKeyError
from prahari_registry.mediamtx import ReconcileResult
from prahari_registry.metrics import CAMERAS, MEDIAMTX_PATHS, METRICS, WORKERS_ALIVE
from prahari_registry.models import (
    Camera,
    CameraCreate,
    Org,
    OrgCreate,
    SyncResult,
)
from prahari_registry.probe import ProbeError, ProbeResult

TOKEN = "test-internal-token"
HEADERS = {"x-internal-token": TOKEN}
CAM_ID = "00000000-0000-0000-0000-0000000000ab"
ORG_ID = "00000000-0000-0000-0000-000000000001"


class FakeRepo:
    """The CameraRepository surface the app uses, scripted per test."""

    def __init__(self) -> None:
        self.cameras: dict[str, Camera] = {}
        self.list_args: dict | None = None
        self.create_calls: list[CameraCreate] = []
        self.update_calls: list[tuple] = []
        self.get_by_external_result: Camera | None = None
        self.get_by_external_calls: list[tuple] = []
        self.create_error: Exception | None = None
        self.update_error: Exception | None = None
        self.counts: dict[str, int] = {"active": 5, "absent": 1, "decommissioned": 2}
        self.health = {"healthy": 3, "degraded": 1, "unreachable": 1, "tampered": 0, "unknown": 0}
        self.paths: dict[str, str] = {"cam-1": "rtsp://u:p@dvr/1"}
        self.sync_runs: list[SyncResult] = []
        self.pruned: list[int] = []

    async def get(self, camera_id: str, *, scope: str) -> Camera | None:
        return self.cameras.get(camera_id)

    async def get_by_external(self, source: str, external_id: str, *, scope: str):
        self.get_by_external_calls.append((source, external_id, scope))
        return self.get_by_external_result

    async def list(self, **kwargs):
        self.list_args = kwargs
        return list(self.cameras.values())

    async def count(self, *, scope: str, lifecycle=None) -> int:
        return self.counts[lifecycle.value if lifecycle else "all"]

    async def health_summary(self, *, scope: str) -> dict:
        return dict(self.health)

    async def create(self, payload: CameraCreate) -> Camera:
        if self.create_error is not None:
            raise self.create_error
        self.create_calls.append(payload)
        return Camera(id=CAM_ID, source=payload.source, external_id=payload.external_id)

    async def update(self, camera_id: str, payload, *, scope: str):
        if self.update_error is not None:
            raise self.update_error
        self.update_calls.append((camera_id, payload, scope))
        return self.cameras.get(camera_id)

    async def decommission(self, camera_id: str, *, scope: str):
        return self.cameras.get(camera_id)

    async def last_sync_runs(self, limit: int = 10):
        return self.sync_runs[:limit]

    async def recent_health_history(self, camera_id: str, *, window_s: int, limit: int):
        return [], []

    async def record_heartbeat(self, camera_id, heartbeat, verdict) -> None:
        pass

    async def desired_mediamtx_paths(self) -> dict[str, str]:
        return dict(self.paths)

    async def prune_heartbeats(self, *, retention_days: int) -> int:
        self.pruned.append(retention_days)
        return len(self.pruned)


class FakeOrgRepo:
    def __init__(self) -> None:
        self.orgs: dict[str, Org] = {}
        self.created: list[OrgCreate] = []
        self.create_error: Exception | None = None

    async def get(self, org_id: str) -> Org | None:
        return self.orgs.get(org_id)

    async def create(self, payload: OrgCreate) -> Org:
        if self.create_error is not None:
            raise self.create_error
        org = Org(
            id=ORG_ID,
            parent_id=payload.parent_id,
            path=payload.label,
            kind=payload.kind,
            name=payload.name,
        )
        self.created.append(payload)
        self.orgs[org.id] = org
        return org

    async def list_subtree(self, scope: str):
        return list(self.orgs.values())


class FakeWorkerRepo:
    def __init__(self) -> None:
        self.workers = ["w1", "w2"]
        self.reaped: int = 0
        self.prune_calls: int = 0

    async def alive_worker_ids(self, conn=None):
        return list(self.workers)

    async def prune_stale(self) -> int:
        self.prune_calls += 1
        return self.reaped


class FakePool:
    """Serves `fetchval` (readyz) and `fetch` (gaps) from scripts."""

    def __init__(self) -> None:
        self.fetchval_error: Exception | None = None
        self.fetch_results: list[list[dict]] = []
        self.queries: list[tuple[str, tuple]] = []
        self.closed = False

    async def fetchval(self, sql, *args):
        if self.fetchval_error is not None:
            raise self.fetchval_error
        return 1

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        return self.fetch_results.pop(0) if self.fetch_results else []

    async def close(self) -> None:
        self.closed = True


class FakeSync:
    def __init__(self) -> None:
        self.result: SyncResult | None = SyncResult(
            source="gw", ok=True, started_at=datetime.now(UTC)
        )

    async def run_once_locked(self):
        return self.result


class FakeMediaMTX:
    def __init__(self) -> None:
        self.reconcile_result = ReconcileResult(added=2, removed=1)
        self.reconcile_calls: list[dict] = []

    async def reconcile(self, desired):
        self.reconcile_calls.append(desired)
        return self.reconcile_result


class FakeTicketVerifier:
    async def allows(self, token, *, action, path):
        return False

    async def aclose(self) -> None:
        pass


@pytest.fixture
def state():
    """Install every dependency the routes touch — the same set the lifespan
    would wire, so any endpoint is reachable from any test here."""
    repo, org_repo, worker_repo, pool = FakeRepo(), FakeOrgRepo(), FakeWorkerRepo(), FakePool()
    app.state.settings = RegistrySettings(internal_token=TOKEN, sync_enabled=False)
    app.state.repo = repo
    app.state.org_repo = org_repo
    app.state.worker_repo = worker_repo
    app.state.pool = pool
    app.state.sync = FakeSync()
    app.state.mediamtx = FakeMediaMTX()
    app.state.ticket_verifier = FakeTicketVerifier()
    app.state.gateway_configured = True
    return repo


@pytest.fixture
def client(state) -> TestClient:
    return TestClient(app)


def _camera() -> Camera:
    return Camera(id=CAM_ID, source="manual", external_id="cam-x")


# --- probes / metrics ---------------------------------------------------------


def test_readyz_reports_ready_when_the_db_answers(state, client):
    resp = client.get("/readyz")
    assert resp.status_code == 200 and resp.json()["database"] == "ok"


def test_readyz_reports_unavailable_without_leaking_db_internals(state, client):
    """The endpoint answers unauthenticated callers — the error text can carry
    hostnames and query fragments, so it is logged, not returned."""
    app.state.pool.fetchval_error = OSError("connection to db.internal:5432 refused")
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert "db.internal" not in resp.text


def test_metrics_is_exempt_from_the_token_gate(state, client):
    """Prometheus cannot hold the credential — same reason /healthz is exempt."""
    resp = client.get("/metrics")  # no X-Internal-Token
    assert resp.status_code == 200
    assert f"{WORKERS_ALIVE} 2" in resp.text
    assert f"{CAMERAS}_healthy 3" in resp.text
    assert f"{MEDIAMTX_PATHS} 1" in resp.text


def test_metrics_serves_last_known_values_when_the_refresh_fails(state, client):
    """A scrape is not worth a 500 — during a DB blip the last-known gauges
    (and their staleness) are the signal."""

    class DeadRepo(FakeRepo):
        async def health_summary(self, *, scope: str):
            raise OSError("db gone")

    app.state.repo = DeadRepo()
    resp = client.get("/metrics")
    assert resp.status_code == 200


def test_heartbeats_advance_the_received_counter(state, client):
    state.cameras[CAM_ID] = _camera()
    before = METRICS.get("prahari_registry_heartbeats_received_total")
    client.post(
        f"/api/v1/cameras/{CAM_ID}/heartbeat",
        headers=HEADERS,
        json={"worker_id": "w1", "connected": True},
    )
    assert METRICS.get("prahari_registry_heartbeats_received_total") == before + 1


# --- orgs ----------------------------------------------------------------------


def test_create_org_returns_201(state, client):
    resp = client.post(
        "/api/v1/orgs",
        headers=HEADERS,
        json={"label": "amd", "kind": "local_body", "name": "Ahmedabad"},
    )
    assert resp.status_code == 201
    assert resp.json()["path"] == "amd"


def test_create_org_with_a_missing_parent_is_404(state, client):
    app.state.org_repo.create_error = ValueError("no org 'xyz'")
    resp = client.post(
        "/api/v1/orgs",
        headers=HEADERS,
        json={"parent_id": ORG_ID, "label": "amd", "kind": "local_body", "name": "Amd"},
    )
    assert resp.status_code == 404


def test_list_orgs_returns_the_subtree(state, client):
    app.state.org_repo.orgs[ORG_ID] = Org(id=ORG_ID, path="gj", kind="state", name="Gujarat")
    resp = client.get("/api/v1/orgs", headers=HEADERS)
    assert resp.status_code == 200 and resp.json()[0]["path"] == "gj"


def test_get_org_404s_when_absent(state, client):
    assert client.get(f"/api/v1/orgs/{ORG_ID}", headers=HEADERS).status_code == 404


def test_get_org_returns_the_org(state, client):
    app.state.org_repo.orgs[ORG_ID] = Org(id=ORG_ID, path="gj", kind="state", name="Gujarat")
    resp = client.get(f"/api/v1/orgs/{ORG_ID}", headers=HEADERS)
    assert resp.status_code == 200 and resp.json()["name"] == "Gujarat"


# --- cameras -------------------------------------------------------------------


def test_list_cameras_passes_every_filter_to_the_repo(state, client):
    resp = client.get(
        "/api/v1/cameras",
        headers=HEADERS,
        params={
            "district": "Ahmedabad",
            "department": "Traffic",
            "state": "degraded",
            "lifecycle": "absent",
            "bbox": "72.0,23.0,73.0,24.0",
            "search": "junction",
            "limit": 25,
            "offset": 5,
        },
    )
    assert resp.status_code == 200
    args = state.list_args
    assert args["bbox"] == (72.0, 23.0, 73.0, 24.0)
    assert args["state"].value == "degraded" and args["lifecycle"].value == "absent"
    assert (args["limit"], args["offset"]) == (25, 5)


def test_list_cameras_rejects_a_malformed_bbox(state, client):
    """`min_lon,min_lat,max_lon,max_lat` — the ordering that silently puts a
    district in the wrong hemisphere when got wrong, so it is validated hard."""
    assert (
        client.get("/api/v1/cameras", headers=HEADERS, params={"bbox": "1,2,3"}).status_code == 422
    )
    assert (
        client.get("/api/v1/cameras", headers=HEADERS, params={"bbox": "1,2,three,4"}).status_code
        == 422
    )


def test_camera_summary_counts_by_lifecycle_and_health(state, client):
    resp = client.get("/api/v1/cameras/summary", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert (body["active"], body["absent"], body["decommissioned"]) == (5, 1, 2)
    assert body["health"]["healthy"] == 3


def test_cameras_geojson_returns_a_feature_collection(state, client):
    app.state.pool.fetch_results.append(
        [
            {
                "id": CAM_ID,
                "site_name": "Zone 4",
                "district": "Ahmedabad",
                "department": None,
                "latitude": 23.0,
                "longitude": 72.5,
                "state": "healthy",
                "reason": None,
                "observed_fps": 8.0,
                "last_frame_at": None,
            }
        ]
    )
    resp = client.get(
        "/api/v1/cameras/geojson", headers=HEADERS, params={"bbox": "72.0,23.0,73.0,24.0"}
    )
    assert resp.status_code == 200
    feature = resp.json()["features"][0]
    assert feature["geometry"]["coordinates"] == [72.5, 23.0]


def test_create_camera_under_an_explicit_org(state, client):
    app.state.org_repo.orgs[ORG_ID] = Org(
        id=ORG_ID, path="gj.amd", kind="local_body", name="Ahmedabad"
    )
    resp = client.post(
        "/api/v1/cameras",
        headers=HEADERS,
        json={"external_id": "dvr-1", "org_id": ORG_ID, "rtsp_url": "rtsp://10.0.0.7/ch1"},
    )
    assert resp.status_code == 201
    # The duplicate check runs against the org the camera would land in.
    assert state.get_by_external_calls == [("manual", "dvr-1", "gj.amd")]


def test_create_camera_with_an_unknown_org_is_404(state, client):
    resp = client.post(
        "/api/v1/cameras", headers=HEADERS, json={"external_id": "dvr-1", "org_id": ORG_ID}
    )
    assert resp.status_code == 404


def test_create_camera_defaults_to_the_state_root_org(state, client):
    resp = client.post("/api/v1/cameras", headers=HEADERS, json={"external_id": "dvr-1"})
    assert resp.status_code == 201
    assert state.get_by_external_calls == [("manual", "dvr-1", "gj")]


def test_create_camera_conflict_on_a_duplicate_source_id(state, client):
    state.get_by_external_result = _camera()
    resp = client.post("/api/v1/cameras", headers=HEADERS, json={"external_id": "cam-x"})
    assert resp.status_code == 409


def test_create_camera_credential_key_error_is_a_400(state, client):
    """An operator submitting a credential while PRAHARI_CREDENTIAL_KEY is
    unset is a config error the caller can act on, not a 500."""
    state.create_error = CredentialKeyError("PRAHARI_CREDENTIAL_KEY is not set")
    resp = client.post(
        "/api/v1/cameras",
        headers=HEADERS,
        json={"external_id": "dvr-1", "stream_password": "pw"},
    )
    assert resp.status_code == 400


def test_create_camera_rejects_a_link_local_rtsp_url(state, client):
    """L1: the stored rtsp_url is what MediaMTX later connects to on our
    behalf — write-time validation must apply the same SSRF policy as the
    probe, or the registry stores a connect-anywhere primitive. 169.254.x.x
    is the cloud-metadata range."""
    resp = client.post(
        "/api/v1/cameras",
        headers=HEADERS,
        json={"external_id": "dvr-1", "rtsp_url": "rtsp://169.254.169.254/latest"},
    )
    assert resp.status_code == 400
    assert state.create_calls == []  # nothing was stored


def test_create_camera_rejects_loopback_multicast_and_reserved_targets(state, client):
    for url in (
        "rtsp://127.0.0.1:554/x",
        "rtsp://[::1]:554/x",
        "rtsp://224.0.0.1:554/x",
    ):
        resp = client.post(
            "/api/v1/cameras",
            headers=HEADERS,
            json={"external_id": "dvr-1", "rtsp_url": url},
        )
        assert resp.status_code == 400, url


def test_create_camera_rejects_a_non_rtsp_scheme_and_a_disallowed_port(state, client):
    """The policy is the probe's, not tighter: scheme must be rtsp and the
    port must be in probe_allowed_ports (default {554})."""
    for url in (
        "http://169.254.169.254/latest/meta-data",
        "rtsp://10.0.0.7:6379/ch1",
        "file:///etc/passwd",
    ):
        resp = client.post(
            "/api/v1/cameras",
            headers=HEADERS,
            json={"external_id": "dvr-1", "rtsp_url": url},
        )
        assert resp.status_code == 400, url


def test_create_camera_allows_rfc1918_dvr_addresses(state, client):
    """DVRs legitimately live on private ranges — the probe's policy allows
    them, so registration must not tighten past it."""
    resp = client.post(
        "/api/v1/cameras",
        headers=HEADERS,
        json={"external_id": "dvr-1", "rtsp_url": "rtsp://192.168.10.20:554/ch1"},
    )
    assert resp.status_code == 201
    assert state.create_calls[0].rtsp_url == "rtsp://192.168.10.20:554/ch1"


def test_create_camera_without_an_rtsp_url_skips_validation(state, client):
    resp = client.post("/api/v1/cameras", headers=HEADERS, json={"external_id": "cam-x"})
    assert resp.status_code == 201


def test_get_camera_404s_for_a_camera_outside_scope(state, client):
    assert client.get(f"/api/v1/cameras/{CAM_ID}", headers=HEADERS).status_code == 404


def test_get_camera_returns_the_camera(state, client):
    state.cameras[CAM_ID] = _camera()
    resp = client.get(f"/api/v1/cameras/{CAM_ID}", headers=HEADERS)
    assert resp.status_code == 200 and resp.json()["external_id"] == "cam-x"


def test_list_cameras_without_a_bbox_passes_none(state, client):
    client.get("/api/v1/cameras", headers=HEADERS)
    assert state.list_args["bbox"] is None


def test_update_camera(state, client):
    state.cameras[CAM_ID] = _camera()
    resp = client.patch(f"/api/v1/cameras/{CAM_ID}", headers=HEADERS, json={"district": "Surat"})
    assert resp.status_code == 200
    assert state.update_calls[0][0] == CAM_ID


def test_update_camera_404_and_credential_error(state, client):
    assert (
        client.patch(
            f"/api/v1/cameras/{CAM_ID}", headers=HEADERS, json={"district": "S"}
        ).status_code
        == 404
    )
    state.cameras[CAM_ID] = _camera()
    state.update_error = CredentialKeyError("no key")
    resp = client.patch(f"/api/v1/cameras/{CAM_ID}", headers=HEADERS, json={"stream_password": "x"})
    assert resp.status_code == 400


def test_decommission_camera(state, client):
    state.cameras[CAM_ID] = _camera()
    assert client.delete(f"/api/v1/cameras/{CAM_ID}", headers=HEADERS).status_code == 200
    state.cameras.clear()
    assert client.delete(f"/api/v1/cameras/{CAM_ID}", headers=HEADERS).status_code == 404


def test_probe_endpoint_success_and_failure(monkeypatch, state, client):
    """probe_rtsp itself is exercised end-to-end in test_probe.py; here the
    endpoint only needs to route ProbeError → 200-unreachable and
    SSRFBlockedError → 400."""

    async def ok(url, **kwargs):
        return ProbeResult(reachable=True, status_message="200 OK")

    async def fails(url, **kwargs):
        raise ProbeError("connection refused")

    monkeypatch.setattr(registry_app, "probe_rtsp", ok)
    resp = client.post("/api/v1/cameras/probe", headers=HEADERS, json={"rtsp_url": "rtsp://dvr/1"})
    assert resp.status_code == 200 and resp.json()["reachable"] is True

    monkeypatch.setattr(registry_app, "probe_rtsp", fails)
    resp = client.post("/api/v1/cameras/probe", headers=HEADERS, json={"rtsp_url": "rtsp://dvr/1"})
    assert resp.status_code == 200 and resp.json()["reachable"] is False


# --- sync ----------------------------------------------------------------------


def test_trigger_sync_is_503_without_gateway_credentials(state, client):
    app.state.gateway_configured = False
    resp = client.post("/api/v1/sync", headers=HEADERS)
    assert resp.status_code == 503


def test_trigger_sync_is_409_while_a_sync_is_running(state, client):
    app.state.sync.result = None
    assert client.post("/api/v1/sync", headers=HEADERS).status_code == 409


def test_trigger_sync_returns_the_result(state, client):
    resp = client.post("/api/v1/sync", headers=HEADERS)
    assert resp.status_code == 200 and resp.json()["ok"] is True


def test_sync_runs_lists_recent_passes(state, client):
    app.state.repo.sync_runs = [SyncResult(source="gw", ok=True, started_at=datetime.now(UTC))]
    resp = client.get("/api/v1/sync/runs", headers=HEADERS)
    assert resp.status_code == 200 and resp.json()[0]["source"] == "gw"


# --- fan-out --------------------------------------------------------------------


def test_reconcile_streams_delegates_to_the_mediamtx_client(state, client):
    resp = client.post("/api/v1/streams/reconcile", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {
        "added": 2,
        "updated": 0,
        "removed": 1,
        "failed": 0,
        "skipped_reason": None,
    }
    assert app.state.mediamtx.reconcile_calls == [{"cam-1": "rtsp://u:p@dvr/1"}]


# --- gap analysis -----------------------------------------------------------------


def test_district_coverage_endpoint(state, client):
    app.state.pool.fetch_results.append(
        [
            {
                "district": "Ahmedabad",
                "registered": 4,
                "healthy": 3,
                "degraded": 0,
                "unreachable": 1,
                "tampered": 0,
                "unknown": 0,
                "absent": 0,
            }
        ]
    )
    resp = client.get("/api/v1/gaps/districts", headers=HEADERS)
    assert resp.status_code == 200 and resp.json()[0]["coverage_pct"] == 75.0


def test_dark_zones_endpoint_uses_the_configured_default_radius(state, client):
    app.state.settings = RegistrySettings(
        internal_token=TOKEN, sync_enabled=False, gap_dark_zone_radius_m=750.0
    )
    resp = client.get("/api/v1/gaps/dark-zones", headers=HEADERS)
    assert resp.status_code == 200
    assert app.state.pool.queries[0][1] == ("gj", 750.0)


def test_nearest_endpoint_requires_coordinates(state, client):
    assert client.get("/api/v1/gaps/nearest", headers=HEADERS).status_code == 422
    app.state.pool.fetch_results.append(
        [
            {
                "id": CAM_ID,
                "site_name": "Zone 4",
                "district": "Ahmedabad",
                "latitude": 23.0,
                "longitude": 72.5,
                "state": "healthy",
                "distance_m": 12.34,
            }
        ]
    )
    resp = client.get("/api/v1/gaps/nearest", headers=HEADERS, params={"lat": 23.0, "lon": 72.5})
    assert resp.status_code == 200 and resp.json()[0]["distance_m"] == 12.3


# --- lifespan and background loops ---------------------------------------------------


def test_gateway_settings_or_none_swallows_missing_credentials(monkeypatch):
    """Gateway credentials are optional: the registry has real work without
    them (manual registration, health, gaps), so absence is logged and
    surfaced on /healthz, never fatal."""

    class _Unconfigured:
        def __call__(self):
            raise ValidationError.from_exception_data(
                "GatewaySettings", [{"type": "missing", "loc": ("host",), "input": {}}]
            )

    monkeypatch.setattr(registry_app, "GatewaySettings", _Unconfigured())
    assert registry_app._gateway_settings_or_none() is None

    sentinel = object()
    monkeypatch.setattr(registry_app, "GatewaySettings", lambda: sentinel)
    assert registry_app._gateway_settings_or_none() is sentinel


async def test_retention_loop_prunes_and_reaps_each_pass():
    repo, workers = FakeRepo(), FakeWorkerRepo()
    workers.reaped = 2
    settings = RegistrySettings(
        sync_enabled=False, heartbeat_prune_interval_s=0.01, heartbeat_retention_days=7
    )
    task = asyncio.create_task(registry_app._retention_loop(repo, workers, settings))
    await asyncio.sleep(0.05)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert repo.pruned and all(days == 7 for days in repo.pruned)
    assert workers.prune_calls == len(repo.pruned)


async def test_retention_loop_propagates_cancellation():
    """CancelledError is shutdown, not a failure — it must not be swallowed by
    the loop's keep-going guard."""

    class CancellingRepo(FakeRepo):
        async def prune_heartbeats(self, *, retention_days: int) -> int:
            raise asyncio.CancelledError

    settings = RegistrySettings(sync_enabled=False, heartbeat_prune_interval_s=0.01)
    with pytest.raises(asyncio.CancelledError):
        await registry_app._retention_loop(CancellingRepo(), FakeWorkerRepo(), settings)


async def test_retention_loop_survives_a_failing_prune():
    """A failed DELETE must not kill the loop task and silently end retention —
    same reason the sync loop wraps every pass."""

    class FlakyRepo(FakeRepo):
        async def prune_heartbeats(self, *, retention_days: int) -> int:
            self.pruned.append(retention_days)
            if len(self.pruned) == 1:
                raise RuntimeError("db blip")
            return 0

    repo, workers = FlakyRepo(), FakeWorkerRepo()
    settings = RegistrySettings(sync_enabled=False, heartbeat_prune_interval_s=0.01)
    task = asyncio.create_task(registry_app._retention_loop(repo, workers, settings))
    await asyncio.sleep(0.05)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert len(repo.pruned) > 1  # the loop kept going past the first failure


def test_lifespan_wires_state_and_shuts_down_cleanly(monkeypatch):
    """The whole lifespan with the Postgres touchpoints faked: pool, migration
    runner and the timescale probe. Asserting on app.state inside the context
    pins what the wiring installs."""
    pool = FakePool()
    monkeypatch.setattr(registry_app, "create_pool", _async_returning(pool))
    applied_calls = []

    async def fake_migrations(p):
        applied_calls.append(p)
        return ["001_test"]

    monkeypatch.setattr(registry_app, "apply_migrations", fake_migrations)
    monkeypatch.setattr(registry_app, "timescale_available", _async_returning(True))
    monkeypatch.setattr(
        registry_app,
        "registry_settings",
        lambda: RegistrySettings(internal_token=TOKEN, sync_enabled=False),
    )

    class _Unconfigured:
        def __call__(self):
            raise ValidationError.from_exception_data(
                "GatewaySettings", [{"type": "missing", "loc": ("host",), "input": {}}]
            )

    monkeypatch.setattr(registry_app, "GatewaySettings", _Unconfigured())

    with TestClient(app):
        assert app.state.pool is pool
        assert app.state.gateway_configured is False
        assert app.state.repo._pool is pool  # CameraRepository was built on it
    assert pool.closed
    assert applied_calls == [pool]


def _async_returning(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


def test_lifespan_runs_degraded_without_a_token_or_timescale(monkeypatch, caplog):
    """The two loudly-logged degraded paths: no internal token (auth gate
    off — local default, not fail-closed) and no timescaledb (heartbeat
    retention falls back to the in-process pruner)."""
    pool = FakePool()
    monkeypatch.setattr(registry_app, "create_pool", _async_returning(pool))
    monkeypatch.setattr(registry_app, "apply_migrations", _async_returning([]))
    monkeypatch.setattr(registry_app, "timescale_available", _async_returning(False))
    monkeypatch.setattr(
        registry_app,
        "registry_settings",
        lambda: RegistrySettings(internal_token="", sync_enabled=False),
    )

    with caplog.at_level("WARNING"), TestClient(app):
        pass

    assert "unauthenticated" in caplog.text
    assert "timescaledb not present" in caplog.text


def test_metrics_get_returns_zero_for_a_never_touched_name():
    from prahari_registry.metrics import Metrics

    assert Metrics().get("prahari_registry_never_touched") == 0.0
