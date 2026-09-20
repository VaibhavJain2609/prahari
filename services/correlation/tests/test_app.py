"""app.py: the FastAPI surface. `/healthz` must survive a disconnected
detection consumer without touching it; `/readyz` must actually report
whether one is connected. `/api/v1/routes/{plate}` is exercised through
`dependency_overrides` on `get_store`/`get_registry` rather than a real
Redis-backed store or a real registry HTTP call -- this is an endpoint-wiring
test, not a `build_route` behaviour test (that is `test_routes.py`'s job).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from google.protobuf.timestamp_pb2 import Timestamp
from prahari.v1 import common_pb2, events_pb2

from prahari_correlation.app import app, get_consumer, get_db, get_registry, get_store
from prahari_correlation.registry_client import DarkZone, GeoPoint
from prahari_correlation.store import DetectionStore


def _client() -> TestClient:
    return TestClient(app)


def _detection(camera_id: str, plate_text: str) -> events_pb2.VehicleDetection:
    # `time.time()`, not a fixed epoch: the route endpoint bounds history to
    # `route_history_lookback_s` (24h default), so a detection stamped in 1970
    # would be correctly — confusingly — out of window.
    ts = Timestamp()
    ts.FromDatetime(datetime.fromtimestamp(time.time(), tz=UTC))
    return events_pb2.VehicleDetection(
        detection_id="D1",
        camera_id=camera_id,
        observed_at=common_pb2.StreamTime(wall_clock=ts),
        plate=events_pb2.PlateReading(normalised_text=plate_text),
        evidence_ref="evidence/1",
    )


class _FakeRegistry:
    def __init__(self, locations: dict[str, GeoPoint | None]) -> None:
        self._locations = locations

    async def camera_location(self, camera_id: str) -> GeoPoint | None:
        return self._locations.get(camera_id)

    async def dark_zones(self) -> list[DarkZone]:
        return [DarkZone(camera_id="CAM-DOWN", location=None)]


class TestInternalToken:
    """`require_internal_token`: once PRAHARI_CORRELATION_INTERNAL_TOKEN is
    set, /api/* answers 401 without the header — the route data behind it is
    movement history, exactly what the BFF's authorisation exists to control.
    Unset means the gate is off entirely (local/dev default)."""

    def _armed_client(self, monkeypatch) -> TestClient:
        monkeypatch.setenv("PRAHARI_CORRELATION_INTERNAL_TOKEN", "tok-1")
        return TestClient(app)

    def test_unset_token_leaves_the_api_open(self, monkeypatch) -> None:
        monkeypatch.delenv("PRAHARI_CORRELATION_INTERNAL_TOKEN", raising=False)
        store = DetectionStore(max_per_plate=10, max_plates=10)
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_registry] = lambda: _FakeRegistry({})
        try:
            with _client() as client:
                assert client.get("/api/v1/routes/GJ01AB1234").status_code == 200
        finally:
            app.dependency_overrides.clear()

    def test_armed_gate_rejects_a_missing_token(self, monkeypatch) -> None:
        with self._armed_client(monkeypatch) as client:
            response = client.get("/api/v1/routes/GJ01AB1234")
        assert response.status_code == 401

    def test_armed_gate_rejects_a_wrong_token(self, monkeypatch) -> None:
        with self._armed_client(monkeypatch) as client:
            response = client.get(
                "/api/v1/routes/GJ01AB1234", headers={"x-internal-token": "wrong"}
            )
        assert response.status_code == 401

    def test_armed_gate_accepts_the_right_token(self, monkeypatch) -> None:
        store = DetectionStore(max_per_plate=10, max_plates=10)
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_registry] = lambda: _FakeRegistry({})
        try:
            with self._armed_client(monkeypatch) as client:
                response = client.get(
                    "/api/v1/routes/GJ01AB1234", headers={"x-internal-token": "tok-1"}
                )
        finally:
            app.dependency_overrides.clear()
        assert response.status_code == 200

    def test_healthz_stays_open_when_armed(self, monkeypatch) -> None:
        # A liveness probe carries no data and must not depend on a secret
        # being wired correctly to answer.
        with self._armed_client(monkeypatch) as client:
            assert client.get("/healthz").status_code == 200


class TestProbes:
    def test_healthz_never_touches_the_consumer(self) -> None:
        with _client() as client:
            response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "service": "correlation"}

    def test_readyz_is_unavailable_when_no_redis_is_configured(self) -> None:
        # PRAHARI_CORRELATION_REDIS_URL is unset by default -- the consumer
        # never starts, and /readyz must say so rather than reporting ready.
        with _client() as client:
            response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["status"] == "unavailable"

    def test_readyz_reports_in_memory_persistence_when_no_db_is_configured(
        self,
    ) -> None:
        # Consuming but not persisting is a degraded state, not a failure:
        # ready, with the honesty label attached.
        class _Connected:
            def is_connected(self) -> bool:
                return True

        app.dependency_overrides[get_consumer] = lambda: _Connected()
        try:
            with _client() as client:
                response = client.get("/readyz")
        finally:
            app.dependency_overrides.clear()
        assert response.status_code == 200
        assert response.json() == {"status": "ready", "persistence": "in-memory"}

    def test_readyz_reports_postgres_persistence_when_the_db_pings(self) -> None:
        class _Connected:
            def is_connected(self) -> bool:
                return True

        class _Db:
            async def ping(self) -> bool:
                return True

        app.dependency_overrides[get_consumer] = lambda: _Connected()
        app.dependency_overrides[get_db] = lambda: _Db()
        try:
            with _client() as client:
                response = client.get("/readyz")
        finally:
            app.dependency_overrides.clear()
        assert response.status_code == 200
        assert response.json() == {"status": "ready", "persistence": "postgres"}

    def test_readyz_is_unavailable_when_the_configured_db_is_down(self) -> None:
        # Configured-but-unreachable Postgres means the consumer can only
        # build pending backlog — degraded, and said so.
        class _Connected:
            def is_connected(self) -> bool:
                return True

        class _DownDb:
            async def ping(self) -> bool:
                raise ConnectionError("simulated postgres outage")

        app.dependency_overrides[get_consumer] = lambda: _Connected()
        app.dependency_overrides[get_db] = lambda: _DownDb()
        try:
            with _client() as client:
                response = client.get("/readyz")
        finally:
            app.dependency_overrides.clear()
        assert response.status_code == 503
        assert response.json()["persistence"] == "postgres"


class TestRoutes:
    def test_get_route_serialises_hops_and_dark_zones(self) -> None:
        store = DetectionStore(max_per_plate=10, max_plates=10)
        store.add(_detection("CAM-A", "GJ01AB1234"))
        registry = _FakeRegistry({"CAM-A": GeoPoint(latitude=23.0, longitude=72.5)})

        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_registry] = lambda: registry
        try:
            with _client() as client:
                response = client.get("/api/v1/routes/GJ01AB1234")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        body = response.json()
        assert body["plate"] == "GJ01AB1234"
        assert len(body["hops"]) == 1
        assert body["hops"][0]["camera_id"] == "CAM-A"
        assert body["hops"][0]["location"] == {"latitude": 23.0, "longitude": 72.5}
        assert body["hops"][0]["link_kind"] is None
        assert body["hops"][0]["first_seen_s"] == body["hops"][0]["last_seen_s"]
        assert body["hops"][0]["sightings"] == 1
        assert body["ungated_hops"] == 0
        assert body["dark_zones"] == [{"camera_id": "CAM-DOWN", "location": None}]

    def test_get_route_for_an_unseen_plate_is_an_empty_route(self) -> None:
        store = DetectionStore(max_per_plate=10, max_plates=10)
        registry = _FakeRegistry({})

        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_registry] = lambda: registry
        try:
            with _client() as client:
                response = client.get("/api/v1/routes/GJ99ZZ9999")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        body = response.json()
        assert body["hops"] == []
        assert body["rejected"] == []


class TestMetrics:
    def test_metrics_endpoint_is_plaintext_prahari_correlation_lines(self) -> None:
        # The real lifespan store/metrics are exercised here rather than
        # overridden: the gauges are bound to the lifespan's objects, so a
        # dependency-overridden store would be invisible to them.
        registry = _FakeRegistry({"CAM-A": GeoPoint(latitude=23.0, longitude=72.5)})
        app.dependency_overrides[get_registry] = lambda: registry
        try:
            with _client() as client:
                response = client.get("/metrics")
                client.app.state.store.add(_detection("CAM-A", "GJ01AB1234"))
                # One route query so a request-side counter has something to say.
                client.get("/api/v1/routes/GJ01AB1234")
                after = client.get("/metrics")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        text = after.text
        # Store-side counters and gauges.
        assert "prahari_correlation_detections_consumed 1" in text
        assert "prahari_correlation_store_plates 1" in text
        assert "prahari_correlation_store_unplated 0" in text
        # Request-side counters.
        assert "prahari_correlation_route_queries 1" in text
        # No Redis configured -> the stream-length gauge is omitted rather
        # than rendered as a misleading 0.
        assert "detections_stream_length" not in text
