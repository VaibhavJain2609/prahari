"""The Day 3 gate (`docs/DAY3-DESIGN.md` §7): "the full vertical slice runs
on the laptop... give it a registration number, get back a timestamped,
location-wise route." An explicit go/no-go -- it decides whether Day 4 spends
GPU money.

Per the design doc, verbatim: "synthetic detections (including one
impossible-hop injection and one non-watchlist plate) through the detections
stream -> correlation route -> BFF export -> CSV/PDF checked for the injected
plate's true hops and the rejected hop's absence. No model weights, no live
gateway, no k3d cluster required for the gate test itself -- same reasoning
as Day 2's gate: the gate proves wiring, not infrastructure."

What stands in for "the detections stream" here is exactly what
`prahari_correlation.consumer.DetectionConsumer` does with every message it
reads off it: `DetectionStore.add(detection)`. Only the Redis transport
between match-engine's publisher and correlation's consumer is skipped (no
live Redis, per the gate's own "no live gateway" clause) -- everything
downstream of that point is real, unfaked production code:

    synthetic VehicleDetections
      -> DetectionStore.add() (correlation)
      -> the real correlation FastAPI app, over an in-process ASGI transport
      -> CorrelationClient (bff) -> the real BFF FastAPI app
      -> route_to_csv / route_to_pdf (bff/export.py)

Two properties, each its own test:

1. An injected impossible hop (a ~900 km jump in 5 minutes) is excluded from
   the reconstructed route and named in `rejected`, never silently folded in
   as a normal step or silently dropped without a trace.
2. A plate that never touched a watchlist still gets a complete route. This
   is the load-bearing fix DAY3-DESIGN.md §1 opens with: Day 2's bus carried
   only watchlist hits, so route reconstruction for a plate nobody flagged
   was not degraded, it was impossible. Day 3's `prahari:detections` stream
   (§2) is what makes it possible; this test is what proves the fix, not just
   the design note describing it.
"""

from __future__ import annotations

import csv
import io
from contextlib import contextmanager
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from google.protobuf.timestamp_pb2 import Timestamp
from prahari.v1 import common_pb2, events_pb2
from prahari_bff.app import app as bff_app
from prahari_bff.app import get_audit_log, get_correlation_client
from prahari_bff.audit import AuditLog
from prahari_bff.auth import get_principal
from prahari_bff.config import BFFSettings
from prahari_bff.correlation_client import CorrelationClient
from prahari_bff.models import Principal, Role
from prahari_correlation.app import app as correlation_app
from prahari_correlation.app import get_registry as correlation_get_registry
from prahari_correlation.app import get_settings as correlation_get_settings
from prahari_correlation.app import get_store as correlation_get_store
from prahari_correlation.config import CorrelationSettings
from prahari_correlation.registry_client import GeoPoint
from prahari_correlation.store import DetectionStore
from prahari_match.confusion import skeleton
from prahari_match.watchlist import Watchlist

PURPOSE_CODE = "gate-day3-2026"

# A start point in Ahmedabad, a ~10.5 km hop 30 minutes later (feasible at
# ~21 km/h), then an injected ~915 km jump to Delhi only 5 minutes after that
# (implies ~11,000 km/h -- rejected by the default 120 km/h envelope).
CAM_A = ("CAM-A", GeoPoint(latitude=23.03, longitude=72.58))
CAM_B = ("CAM-B", GeoPoint(latitude=23.10, longitude=72.65))
CAM_C = ("CAM-C", GeoPoint(latitude=28.6139, longitude=77.2090))

# A second, independent plate/route -- close cameras, both hops feasible --
# used only to prove route reconstruction works for a plate that was never
# added to any watchlist.
CAM_D = ("CAM-D", GeoPoint(latitude=22.30, longitude=70.80))
CAM_E = ("CAM-E", GeoPoint(latitude=22.35, longitude=70.85))

PLATE_TRUE = "GJ01AB1234"
PLATE_GHOST = "GJ05ZZ9999"

BASE_S = 1_700_000_000.0


def _detection(camera_id: str, plate_text: str, wall_clock_s: float, evidence_ref: str):
    ts = Timestamp()
    ts.FromDatetime(datetime.fromtimestamp(wall_clock_s, tz=UTC))
    return events_pb2.VehicleDetection(
        detection_id=f"{camera_id}-{plate_text}",
        camera_id=camera_id,
        observed_at=common_pb2.StreamTime(wall_clock=ts),
        plate=events_pb2.PlateReading(normalised_text=plate_text),
        evidence_ref=evidence_ref,
    )


def _true_route_detections() -> list[events_pb2.VehicleDetection]:
    return [
        _detection(CAM_A[0], PLATE_TRUE, BASE_S, "evidence/cam-a/1"),
        _detection(CAM_B[0], PLATE_TRUE, BASE_S + 1800, "evidence/cam-b/1"),
        _detection(CAM_C[0], PLATE_TRUE, BASE_S + 2100, "evidence/cam-c/1"),
    ]


def _ghost_route_detections() -> list[events_pb2.VehicleDetection]:
    return [
        _detection(CAM_D[0], PLATE_GHOST, BASE_S, "evidence/cam-d/1"),
        _detection(CAM_E[0], PLATE_GHOST, BASE_S + 1800, "evidence/cam-e/1"),
    ]


class _FakeRegistry:
    """Stands in for `RegistryClient`: fixed camera locations, no dark
    zones. Same shape as `services/correlation/tests/test_app.py`'s own
    `_FakeRegistry` -- this gate test reuses that established double rather
    than inventing a second one."""

    def __init__(self, locations: dict[str, GeoPoint]) -> None:
        self._locations = locations

    async def camera_location(self, camera_id: str) -> GeoPoint | None:
        return self._locations.get(camera_id)

    async def dark_zones(self) -> list:
        return []


@pytest.fixture
def wired_store() -> DetectionStore:
    store = DetectionStore(max_per_plate=50, max_plates=50)
    for detection in _true_route_detections() + _ghost_route_detections():
        store.add(detection)
    return store


@contextmanager
def _bff_client(store: DetectionStore, tmp_path):
    """Wires the real correlation app (dependency-overridden store/registry,
    no Redis) behind an in-process ASGI transport, points the real BFF app's
    `CorrelationClient` at it, and overrides the BFF's principal/audit
    dependencies so the whole chain runs with no Postgres, no Redis, and no
    open socket -- exactly the gate's "no live gateway, no k3d cluster"
    requirement. A bare `TestClient(bff_app)` (no `with`) never runs the
    app's own lifespan (see `tests/test_day2_gate.py`'s own note on this),
    which is what lets the BFF app be exercised with no real database pool.
    """
    registry = _FakeRegistry(dict([CAM_A, CAM_B, CAM_C, CAM_D, CAM_E]))
    correlation_app.dependency_overrides[correlation_get_store] = lambda: store
    correlation_app.dependency_overrides[correlation_get_registry] = lambda: registry
    correlation_app.dependency_overrides[correlation_get_settings] = lambda: CorrelationSettings()

    correlation_transport = httpx.ASGITransport(app=correlation_app)
    correlation_http_client = httpx.AsyncClient(
        transport=correlation_transport, base_url="http://correlation.internal"
    )
    correlation_client = CorrelationClient(BFFSettings(), client=correlation_http_client)

    audit = AuditLog(str(tmp_path / "audit.db"))
    principal = Principal(
        id="gate-user",
        subject="gate-tester",
        org_id="org-root",
        org_path="gj",
        role=Role.ADMIN,
        kind="session",
    )

    bff_app.dependency_overrides[get_principal] = lambda: principal
    bff_app.dependency_overrides[get_correlation_client] = lambda: correlation_client
    bff_app.dependency_overrides[get_audit_log] = lambda: audit

    try:
        yield TestClient(bff_app), audit
    finally:
        bff_app.dependency_overrides.clear()
        correlation_app.dependency_overrides.clear()
        audit.close()


async def test_injected_impossible_hop_is_rejected_not_silently_dropped(wired_store, tmp_path):
    with _bff_client(wired_store, tmp_path) as (client, audit):
        headers = {"X-Purpose-Code": PURPOSE_CODE}

        route = client.get(f"/api/v1/routes/{PLATE_TRUE}", headers=headers)
        assert route.status_code == 200
        body = route.json()

        hop_cameras = [hop["camera_id"] for hop in body["hops"]]
        assert hop_cameras == [CAM_A[0], CAM_B[0]], (
            "the true, feasible hops must survive in order; the injected "
            "~900km/5min jump to CAM-C must not appear as a hop"
        )
        assert len(body["rejected"]) == 1
        rejected = body["rejected"][0]
        assert rejected["from_camera_id"] == CAM_B[0]
        assert rejected["to_camera_id"] == CAM_C[0]
        assert rejected["implied_speed_kmh"] > 120.0

        csv_response = client.get(
            f"/api/v1/routes/{PLATE_TRUE}/export", params={"format": "csv"}, headers=headers
        )
        assert csv_response.status_code == 200
        rows = list(csv.reader(io.StringIO(csv_response.text)))
        camera_column = [row[1] for row in rows[1:]]
        assert camera_column == [CAM_A[0], CAM_B[0]], (
            "the export is what an evaluator actually reads -- the injected "
            "hop's absence from the route must hold in the exported file "
            "too, not only in the raw JSON"
        )

        pdf_response = client.get(
            f"/api/v1/routes/{PLATE_TRUE}/export", params={"format": "pdf"}, headers=headers
        )
        assert pdf_response.status_code == 200
        assert pdf_response.content.startswith(b"%PDF-")

        ok, first_broken_id = await audit.verify()
        assert (ok, first_broken_id) == (True, None)


def test_route_for_a_plate_never_added_to_any_watchlist(wired_store, tmp_path):
    watchlist = Watchlist()
    watchlist.add(events_pb2.WatchlistEntry(entry_id="W1", plate=PLATE_TRUE))
    assert skeleton(PLATE_GHOST) not in set(watchlist.skeletons()), (
        "the test setup must actually keep this plate off the watchlist, "
        "not merely assume it -- otherwise this test proves nothing about "
        "the Day 2 -> Day 3 fix it exists to check"
    )

    with _bff_client(wired_store, tmp_path) as (client, _audit):
        headers = {"X-Purpose-Code": PURPOSE_CODE}

        route = client.get(f"/api/v1/routes/{PLATE_GHOST}", headers=headers)
        assert route.status_code == 200
        body = route.json()
        hop_cameras = [hop["camera_id"] for hop in body["hops"]]
        assert hop_cameras == [CAM_D[0], CAM_E[0]], (
            "Day 2's bus only carried watchlist hits, so a plate nobody "
            "flagged had no route at all; Day 3's detections stream must "
            "make this plate's full route reconstructable regardless"
        )
        assert body["rejected"] == []

        csv_response = client.get(
            f"/api/v1/routes/{PLATE_GHOST}/export", params={"format": "csv"}, headers=headers
        )
        assert csv_response.status_code == 200
        rows = list(csv.reader(io.StringIO(csv_response.text)))
        assert [row[1] for row in rows[1:]] == [CAM_D[0], CAM_E[0]]
        assert all(row[0] == PLATE_GHOST for row in rows[1:])


def test_purpose_code_is_mandatory_on_the_export_path(wired_store, tmp_path):
    """`DAY3-DESIGN.md` §4.2: absent `X-Purpose-Code` is 400, never a
    default -- an evidence export with no stated reason is not an
    evidence export."""
    with _bff_client(wired_store, tmp_path) as (client, _audit):
        response = client.get(f"/api/v1/routes/{PLATE_TRUE}/export", params={"format": "csv"})
        assert response.status_code == 400
