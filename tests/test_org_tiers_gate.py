"""The org-tiers gate (`docs/ORG-TIERS-DESIGN.md` §6): "a user signed in at
any of the three org depths sees exactly the cameras in their subtree —
provably, by asserting absent ids in the response payload, not by hiding
rows in the UI."

Same construction as `tests/test_day3_gate.py`: the real BFF FastAPI app
over an in-process `TestClient`, with `dependency_overrides` at the seams
the app already exposes (`get_principal`, `get_registry_client`,
`get_scope_resolver`, `get_audit_log`, `get_correlation_client`). A bare
`TestClient(bff_app)` (no `with`) never runs the app's lifespan, so no
Postgres pool is ever opened — the gate runs hermetically.

What is real here and what is not:

  * REAL: the BFF app itself — `_scoped_params` forcing `org_scope` to the
    caller's own org, `_check_target_org`, the 403-plus-`denied`-audit-entry
    boundary on camera detail, `CameraScopeResolver`'s root-scoped lookup,
    `in_scope` (the Python mirror of `path <@ scope`), the purpose-code
    dependency, and the real hash-chained `AuditLog` on a temp file.
  * FAKED: the registry is a small ASGI double honouring the documented
    scope semantics (`in_scope`, the same function the BFF uses) instead of
    Postgres `ltree`. The actual `<@` predicate and the insert-only `org_id`
    upsert column live in SQL behind `asyncpg.Pool` — there is no hermetic
    seam for them, so the database-level assertions are checked against the
    real repository source where possible and the full end-to-end versions
    are `pytest.mark.skip`ped with the missing seam named. Nothing here
    fakes a pass.
"""

from __future__ import annotations

import inspect
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from prahari_bff.app import (
    app as bff_app,
)
from prahari_bff.app import (
    get_audit_log,
    get_correlation_client,
    get_registry_client,
    get_scope_resolver,
)
from prahari_bff.audit import AuditLog
from prahari_bff.auth import get_principal
from prahari_bff.config import BFFSettings
from prahari_bff.models import Principal, Role
from prahari_bff.registry_client import RegistryClient
from prahari_bff.repository import in_scope
from prahari_bff.scope_resolver import CameraScopeResolver
from prahari_common.catalogue import CatalogueClient
from prahari_registry.config import RegistrySettings
from prahari_registry.repository import CameraRepository
from prahari_registry.sync import CatalogueSync

PURPOSE_CODE = "gate-org-tiers-2026"
HEADERS = {"X-Purpose-Code": PURPOSE_CODE}

# --- the seeded estate (§6 step 1) ------------------------------------------
#
# gj (state) -> gj.ahmedabad_city (organization) -> gj.ahmedabad_city.zone_4
# (local body), plus a fourth org in a different city's subtree so "outside
# the subtree" has somewhere real to point.

ORG_PATHS = {
    "org-gj": "gj",
    "org-amd": "gj.ahmedabad_city",
    "org-z4": "gj.ahmedabad_city.zone_4",
    "org-surat": "gj.surat_city",
}

STATE_ADMIN = Principal(
    id="u-state",
    subject="state.admin",
    org_id="org-gj",
    org_path="gj",
    role=Role.ADMIN,
    kind="session",
)
ORG_VIEWER = Principal(
    id="u-amd",
    subject="amd.viewer",
    org_id="org-amd",
    org_path="gj.ahmedabad_city",
    role=Role.VIEWER,
    kind="session",
)
ZONE4_VIEWER = Principal(
    id="u-z4",
    subject="z4.viewer",
    org_id="org-z4",
    org_path="gj.ahmedabad_city.zone_4",
    role=Role.VIEWER,
    kind="session",
)
ZONE4_OPERATOR = Principal(
    id="u-z4-ops",
    subject="z4.operator",
    org_id="org-z4",
    org_path="gj.ahmedabad_city.zone_4",
    role=Role.OPERATOR,
    kind="session",
)


def _camera(cam_id: str, org_id: str, external_id: str, *, source: str = "gateway") -> dict:
    return {
        "id": cam_id,
        "org_id": org_id,
        "external_id": external_id,
        "source": source,
        "adapter": "manual",
        "site_name": f"Site {external_id}",
        "lifecycle": "active",
    }


def _seed_cameras() -> dict[str, dict]:
    return {
        "cam-state": _camera("cam-state", "org-gj", "GJ-STATE-001"),
        "cam-org": _camera("cam-org", "org-amd", "AMD-002"),
        "cam-zone4": _camera("cam-zone4", "org-z4", "Z4-003"),
        "cam-surat": _camera("cam-surat", "org-surat", "SRT-004"),
    }


# --- the registry double -----------------------------------------------------
#
# Mirrors the real handlers' scope contract (`org_scope` is an ltree path,
# reads return rows under it, unknown or out-of-scope ids are 404) using the
# BFF's own `in_scope` — the documented Python mirror of `path <@ scope`.
# Credential fields are stored under a private key the way the real
# `cameras.stream_secret` column is, and are never present in a response —
# mirroring `Camera`, which has no field for them. That mirroring is the
# assumption under test; the real predicates are asserted at source level in
# `test_..._repository_source` and skipped end-to-end at the bottom.


def _fake_registry(cameras: dict[str, dict]):
    app = FastAPI()
    seen: dict[str, list] = {"list_scopes": [], "posts": []}

    def org_path(cam: dict) -> str:
        return ORG_PATHS[cam["org_id"]]

    def public(cam: dict) -> dict:
        return {k: v for k, v in cam.items() if not k.startswith("_")}

    @app.get("/api/v1/cameras")
    async def list_cameras(org_scope: str = "gj"):
        seen["list_scopes"].append(org_scope)
        return [public(c) for c in cameras.values() if in_scope(org_path(c), org_scope)]

    @app.get("/api/v1/cameras/{camera_id}")
    async def get_camera(camera_id: str, org_scope: str = "gj"):
        cam = cameras.get(camera_id)
        if cam is None or not in_scope(org_path(cam), org_scope):
            raise HTTPException(404, f"no camera {camera_id}")
        return public(cam)

    @app.post("/api/v1/cameras", status_code=201)
    async def create_camera(request: Request):
        body = await request.json()
        seen["posts"].append(dict(body))
        cam = {k: v for k, v in body.items() if k != "stream_password"}
        cam["id"] = f"cam-{body['external_id']}"
        cam["org_id"] = body["org_id"]
        cam["lifecycle"] = "active"
        cam["adapter"] = body.get("adapter", "manual")
        # The plaintext credential is stored server-side (the real registry
        # encrypts it into `stream_secret` via crypto.py — see
        # services/registry/tests/test_credentials.py); it is never a
        # response field.
        cam["_stream_secret"] = body.get("stream_password")
        cameras[cam["id"]] = cam
        return public(cam)

    return app, seen


class _FakePool:
    """`SELECT path FROM orgs WHERE id = $1` and nothing else — the one query
    this service runs against a table it does not own."""

    async def fetchval(self, query: str, org_id: str) -> str | None:
        return ORG_PATHS.get(org_id)


class _FakeCorrelation:
    def __init__(self, route: dict) -> None:
        self._route = route

    async def get_route(self, plate: str) -> dict:
        return self._route


class _Harness:
    def __init__(self, client: TestClient, audit: AuditLog, seen: dict, box: dict) -> None:
        self.client = client
        self.audit = audit
        self.seen = seen
        self._box = box

    def as_principal(self, principal: Principal) -> None:
        """Swap the authenticated caller between requests — several checks
        need the same estate read at different org depths."""
        self._box["principal"] = principal


@contextmanager
def _bff(tmp_path: Path, cameras: dict[str, dict] | None = None, route: dict | None = None):
    registry_app, seen = _fake_registry(cameras if cameras is not None else _seed_cameras())
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=registry_app), base_url="http://registry.internal"
    )
    registry_client = RegistryClient(BFFSettings(), client=http_client)
    pool = _FakePool()
    resolver = CameraScopeResolver(registry_client, pool, root_scope="gj")
    audit = AuditLog(str(tmp_path / "audit.db"))
    box = {"principal": STATE_ADMIN}

    bff_app.state.pool = pool
    bff_app.dependency_overrides[get_principal] = lambda: box["principal"]
    bff_app.dependency_overrides[get_registry_client] = lambda: registry_client
    bff_app.dependency_overrides[get_scope_resolver] = lambda: resolver
    bff_app.dependency_overrides[get_audit_log] = lambda: audit
    if route is not None:
        bff_app.dependency_overrides[get_correlation_client] = lambda: _FakeCorrelation(route)
    try:
        yield _Harness(TestClient(bff_app), audit, seen, box)
    finally:
        bff_app.dependency_overrides.clear()
        audit.close()


def _ids(cameras: list[dict]) -> set[str]:
    return {c["id"] for c in cameras}


# --- §6 steps 2–3: scoped listing, absent ids asserted on the payload --------


def test_zone4_user_lists_only_zone4_cameras(tmp_path):
    """Step 2: the parent orgs' camera ids are ABSENT from the payload — not
    merely unrendered. Asserted on the raw body as well as the parsed ids."""
    with _bff(tmp_path) as h:
        h.as_principal(ZONE4_VIEWER)
        response = h.client.get("/api/v1/cameras")

        assert response.status_code == 200
        assert _ids(response.json()) == {"cam-zone4"}
        for absent in ("cam-state", "cam-org", "cam-surat"):
            assert absent not in response.text


def test_org_user_sees_org_and_zone4_state_user_sees_all(tmp_path):
    """Step 3: scope is a subtree, not a level — `gj.ahmedabad_city` sees its
    own camera and zone-4's, `gj` sees the whole estate."""
    with _bff(tmp_path) as h:
        h.as_principal(ORG_VIEWER)
        response = h.client.get("/api/v1/cameras")
        assert response.status_code == 200
        assert _ids(response.json()) == {"cam-org", "cam-zone4"}
        assert "cam-state" not in response.text
        assert "cam-surat" not in response.text

        h.as_principal(STATE_ADMIN)
        response = h.client.get("/api/v1/cameras")
        assert response.status_code == 200
        assert _ids(response.json()) == {"cam-state", "cam-org", "cam-zone4", "cam-surat"}


def test_a_client_cannot_widen_its_own_scope_with_a_query_param(tmp_path):
    """The scope is the caller's org, never negotiable: an `org_scope` query
    param naming the state root must be stripped and replaced, not honoured
    (`_scoped_params`)."""
    with _bff(tmp_path) as h:
        h.as_principal(ZONE4_VIEWER)
        response = h.client.get("/api/v1/cameras", params={"org_scope": "gj"})

        assert response.status_code == 200
        assert _ids(response.json()) == {"cam-zone4"}
        assert h.seen["list_scopes"] == ["gj.ahmedabad_city.zone_4"], (
            "the registry must have received the caller's own org path, not "
            "the 'gj' the client asked for"
        )


# --- §6 step 4: out-of-scope detail read is 403 plus one audit entry ---------


async def test_out_of_scope_camera_read_is_403_and_audited(tmp_path):
    """403, not the 404 a plain scoped query would give — and the denial is
    itself an audit entry (`action="denied"`): the attempt is evidence too."""
    with _bff(tmp_path) as h:
        h.as_principal(ZONE4_VIEWER)
        response = h.client.get("/api/v1/cameras/cam-surat", headers=HEADERS)

        assert response.status_code == 403
        entries = await h.audit.recent(limit=10)
        assert len(entries) == 1
        entry = entries[0]
        assert entry.action == "denied"
        assert entry.resource == "camera:cam-surat"
        assert entry.actor == ZONE4_VIEWER.subject
        assert entry.org_path == ZONE4_VIEWER.org_path
        assert entry.purpose_code == PURPOSE_CODE


async def test_in_scope_camera_read_is_200_and_audited_as_read(tmp_path):
    """The contrast that keeps the 403 test honest: a camera inside the
    subtree is read normally and logged as `read`."""
    with _bff(tmp_path) as h:
        h.as_principal(ZONE4_VIEWER)
        response = h.client.get("/api/v1/cameras/cam-zone4", headers=HEADERS)

        assert response.status_code == 200
        assert response.json()["id"] == "cam-zone4"
        entries = await h.audit.recent(limit=10)
        assert [(e.action, e.resource) for e in entries] == [("read", "camera:cam-zone4")]


# --- §6 step 5: credential-bearing registration, secret in no response -------


def test_analog_camera_registration_is_scoped_and_never_echoes_its_secret(tmp_path):
    """A zone-4 operator registers a DVR camera; org is forced to the
    caller's own org, the camera is visible to itself and both ancestors, and
    `stream_password` appears in no response body at any tier."""
    secret = "s3cr3t-dvr-password"
    with _bff(tmp_path) as h:
        h.as_principal(ZONE4_OPERATOR)
        created = h.client.post(
            "/api/v1/cameras",
            json={
                "external_id": "Z4-DVR-01",
                "source": "manual",
                "adapter": "rtsp-direct",
                "camera_type": "analog",
                "rtsp_url": "rtsp://dvr.local:554/chn1",
                "stream_username": "admin",
                "stream_password": secret,
            },
        )
        assert created.status_code == 201
        cam_id = created.json()["id"]

        bodies = [created.text]
        # The credential really did reach the registry — "absent from the
        # response" is meaningless if it was dropped before the wire.
        assert h.seen["posts"][-1]["stream_password"] == secret
        # And the org was forced to the caller's own, not trusted from the
        # body (none was sent) and not left for the registry to guess.
        assert h.seen["posts"][-1]["org_id"] == "org-z4"

        for principal in (ZONE4_VIEWER, ORG_VIEWER, STATE_ADMIN):
            h.as_principal(principal)
            listed = h.client.get("/api/v1/cameras")
            assert cam_id in _ids(listed.json()), (
                f"camera at gj.ahmedabad_city.zone_4 must be visible to {principal.org_path}"
            )
            bodies.append(listed.text)
            detail = h.client.get(f"/api/v1/cameras/{cam_id}", headers=HEADERS)
            assert detail.status_code == 200
            bodies.append(detail.text)

        for body in bodies:
            assert secret not in body
            assert "stream_secret" not in body
            assert "stream_password" not in body


def test_zone4_operator_cannot_register_into_a_foreign_org(tmp_path):
    """A supplied `org_id` outside the caller's subtree is 403 — the same
    `_check_target_org` the admin endpoints use, applied to camera writes."""
    with _bff(tmp_path) as h:
        h.as_principal(ZONE4_OPERATOR)
        response = h.client.post(
            "/api/v1/cameras",
            json={"external_id": "Z4-ESCAPE", "org_id": "org-surat"},
        )
        assert response.status_code == 403
        assert h.seen["posts"] == [], "a rejected write must never reach the registry"


# --- §6 step 6: a catalogue sync must not reassign or retire local cameras ---


class _SyncRepo:
    """Records the call contract `CatalogueSync` upholds. Same style as
    `services/registry/tests/test_sync.py`'s FakeRepo — the SQL itself is
    asserted separately below, so this fake never implements the invariant
    it checks."""

    def __init__(self) -> None:
        self.upserts: list[dict] = []
        self.absent_call: dict | None = None

    async def start_sync_run(self, source: str) -> int:
        return 1

    async def finish_sync_run(self, run_id: int, result) -> None:
        pass

    async def org_id_for_path(self, path: str) -> str | None:
        return f"orgid-for-{path}"

    async def upsert_from_catalogue(self, conn, **kwargs) -> tuple[str, bool]:
        self.upserts.append(kwargs)
        return f"uuid-{kwargs['external_id']}", True

    async def mark_absent(self, conn, *, source: str, seen_ids) -> int:
        self.absent_call = {"source": source, "seen_ids": list(seen_ids)}
        return 0

    async def desired_mediamtx_paths(self) -> dict[str, str]:
        return {}


class _FakeConn:
    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSyncPool:
    def acquire(self):
        return _FakeConn()


class _FakeMediaMTX:
    async def reconcile(self, desired):
        return None

    async def aclose(self):
        return None


_SNAPSHOT = {
    "fetched_at": "2026-09-01T10:00:00+00:00",
    "camera_count": 1,
    "live_count": 1,
    "codec_mix": {"h264": 1},
    "cameras": [
        {
            "id": "201",
            "name": "Gateway Cam",
            "latitude": 23.0,
            "longitude": 72.5,
            "live": True,
            "codec": "H264",
        }
    ],
}


async def test_sync_marks_absent_only_within_its_own_source(tmp_path):
    """The locally-registered camera (`source="manual"`) can never be swept
    by `mark_absent` because the sweep is source-scoped — asserted on the
    real `CatalogueSync.run_once` call contract, and on the real SQL below."""
    snapshot_path = tmp_path / "catalogue.json"
    snapshot_path.write_text(json.dumps(_SNAPSHOT))
    catalogue = CatalogueClient.load_snapshot(snapshot_path)

    repo = _SyncRepo()
    sync = CatalogueSync(
        pool=_FakeSyncPool(),
        repo=repo,
        settings=RegistrySettings(catalogue_source="test-gateway"),
        gateway=None,
        mediamtx=_FakeMediaMTX(),
    )
    result = await sync.run_once(catalogue)

    assert result.ok
    assert repo.absent_call["source"] == "test-gateway", (
        "mark_absent must be scoped to the catalogue source — a broader sweep "
        "would retire every manually-registered camera on the first sync"
    )
    assert all(u["default_org_id"] == "orgid-for-gj" for u in repo.upserts), (
        "synced inserts land at sync_default_org_path; reassignment back is "
        "what the SQL below must refuse"
    )


def test_sync_upsert_sql_writes_org_id_on_insert_only():
    """The half of step 6 that lives in SQL, asserted against the real
    repository source: `org_id` is in the INSERT column list but absent from
    the ON CONFLICT SET clause, and `mark_absent` filters `WHERE source`."""
    upsert_src = inspect.getsource(CameraRepository.upsert_from_catalogue)
    insert_part, _, conflict_part = upsert_src.partition("DO UPDATE SET")
    # Word-boundary match: `default_org_id` must not count as `org_id`.
    assert re.search(r"\borg_id\b", insert_part)
    assert not re.search(r"\borg_id\b", conflict_part), (
        "a sync must never move a camera a local body has claimed — "
        "org_id stays out of the DO UPDATE SET list on purpose"
    )

    absent_src = inspect.getsource(CameraRepository.mark_absent)
    assert "WHERE source = $1" in absent_src


@pytest.mark.skip(
    reason=(
        "needs real Postgres + ltree (migration 005): the `<@` scope predicate "
        "and the insert-only `org_id` upsert live in CameraRepository SQL behind "
        "asyncpg.Pool — there is no hermetic seam. Run under `make up`: seed the "
        "three orgs, register a manual camera at gj.ahmedabad_city.zone_4, run "
        "POST /api/v1/sync, assert it is still 'active' and still org-owned by "
        "zone_4. The contract this hinges on is asserted above: source-scoped "
        "mark_absent (run_once call + real SQL) and insert-only org_id (real SQL)."
    )
)
async def test_sync_leaves_a_local_camera_untouched_against_real_postgres():
    pass


@pytest.mark.skip(
    reason=(
        "needs real Postgres + ltree: the scoped-read guarantee this gate's "
        "fake registry mirrors with `in_scope` is `o.path <@ $scope::ltree` in "
        "CameraRepository.list/get — verifiable only against a live database. "
        "Run under `make up`."
    )
)
async def test_scoped_reads_hold_under_the_real_ltree_predicate():
    pass


# --- §6 step 7: breaking the audit chain is detected and named ---------------


async def test_a_broken_audit_link_is_named_by_verify(tmp_path):
    """Tamper with one stored entry directly at the storage layer — the thing
    a verifier exists to catch — and `/api/v1/audit/verify` names that entry."""
    with _bff(tmp_path) as h:
        h.as_principal(STATE_ADMIN)
        for cam in ("cam-state", "cam-zone4", "cam-surat"):
            response = h.client.get(f"/api/v1/cameras/{cam}", headers=HEADERS)
            assert response.status_code == 200

        ok, broken = await h.audit.verify()
        assert (ok, broken) == (True, None)

        conn = sqlite3.connect(h.audit._db_path)
        conn.execute("UPDATE audit_log SET resource = 'camera:cam-tampered' WHERE id = 2")
        conn.commit()
        conn.close()

        verify = h.client.get("/api/v1/audit/verify")
        assert verify.status_code == 200
        assert verify.json() == {"ok": False, "first_broken_entry": 2}


def test_audit_verify_requires_admin(tmp_path):
    """The verification endpoint is admin-gated: a viewer must not be able
    to probe the chain's integrity surface."""
    with _bff(tmp_path) as h:
        h.as_principal(ZONE4_VIEWER)
        assert h.client.get("/api/v1/audit/verify").status_code == 403


# --- §6 tail: the mandatory path stays unfiltered by org scope ---------------


async def test_plate_route_crosses_org_boundaries_unfiltered(tmp_path):
    """`app.py`'s deliberate exception, verified rather than assumed: a route
    is the record of one plate crossing org boundaries, so hops are never
    redacted by the caller's scope — a zone-4 user still gets the hop that
    happened on a Surat camera. Access control is auth + purpose code +
    audit, not filtering."""
    route = {
        "plate": "GJ01AB1234",
        "hops": [
            {
                "camera_id": "cam-zone4",
                "location": "Zone 4 Junction",
                "wall_clock_s": 1700000000.0,
                "link_kind": "seen",
                "confidence": 0.9,
            },
            {
                "camera_id": "cam-surat",
                "location": "Surat Ring Road",
                "wall_clock_s": 1700003600.0,
                "link_kind": "seen",
                "confidence": 0.88,
            },
        ],
        "rejected": [],
        "dark_zones": [],
    }
    with _bff(tmp_path, route=route) as h:
        h.as_principal(ZONE4_VIEWER)
        response = h.client.get("/api/v1/routes/GJ01AB1234", headers=HEADERS)

        assert response.status_code == 200
        hop_cameras = [hop["camera_id"] for hop in response.json()["hops"]]
        assert hop_cameras == ["cam-zone4", "cam-surat"], (
            "the out-of-scope hop must be present — filtering it would "
            "silently break the mandatory plate→route test case"
        )
        entries = await h.audit.recent(limit=5)
        assert [(e.action, e.resource) for e in entries] == [("read", "route:GJ01AB1234")]
