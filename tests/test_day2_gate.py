"""The Day 2 gate (`docs/DAY2-DESIGN.md`, TODO.md "Day 2"): "a known plate in
a clip produces an alert in under 5 s, end to end, with a match explanation
attached." Asserted by a test, not by eyes -- the design doc says so
explicitly, since the web console does not exist until Day 3.

This is integration glue, not a new unit under test: every seam it drives is
already covered in isolation by `services/inference` and
`services/match-engine`'s own suites. What only this file proves is that they
compose -- real production code end to end, nothing faked except the two
model backends `detect/types.py` already ships a scripted double for:

    synthetic clip
      -> DetectionPipeline (ScriptedVehicleDetector + ScriptedPlateReader)
      -> MatchEngineClient, over a real gRPC socket
      -> MetadataIngestServicer -> matcher.match (real, unfaked)
      -> RecentAlertsPublisher
      -> GET /api/v1/alerts (the real FastAPI route)

No k8s, no RTSP, no torch/paddleocr on disk, no network beyond loopback.
"""

from __future__ import annotations

import socket
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from prahari.v1 import events_pb2
from prahari_inference.config import DetectorSettings
from prahari_inference.detect.pipeline import DetectionPipeline
from prahari_inference.detect.plates import ScriptedPlateReader
from prahari_inference.detect.types import PlateCandidate, SampledFrame, VehicleBox
from prahari_inference.detect.vehicles import ScriptedVehicleDetector
from prahari_inference.grpc_client import MatchEngineClient
from prahari_inference.timing import FrameTiming
from prahari_match.alerts import RecentAlertsPublisher
from prahari_match.app import app as match_app
from prahari_match.bloom import BloomFilter
from prahari_match.config import MatchSettings
from prahari_match.dedup import Deduper
from prahari_match.grpc_server import serve
from prahari_match.matcher import WatchlistStore
from prahari_match.watchlist import Watchlist

KNOWN_PLATE = "GJ01AB1234"
GATE_BUDGET_S = 5.0


def _free_port() -> int:
    # Bind-then-close rather than a fixed port: this suite runs inside
    # `make test`'s single `pytest -q` alongside services/match-engine's own
    # gRPC-server tests, and a hardcoded port would race them for it.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _watchlist_store(*plates: str) -> WatchlistStore:
    watchlist = Watchlist()
    for i, plate in enumerate(plates):
        watchlist.add(events_pb2.WatchlistEntry(entry_id=f"W{i}", plate=plate))
    bloom = BloomFilter(expected_items=100)
    for key in watchlist.bloom_keys():
        bloom.add(key)
    return WatchlistStore(watchlist, bloom)


def _synthetic_clip(plate_text: str) -> list[events_pb2.VehicleDetection]:
    """One sampled frame, one vehicle, one legible plate -- "a known plate in
    a clip" with no camera, RTSP or codec involved, run through the real
    `DetectionPipeline` (motion gate off: a single frame has nothing to diff
    against, and that gate is `test_pipeline.py`'s concern, not this one's).
    """
    vehicle = VehicleBox(0.1, 0.1, 0.9, 0.9, "car", 0.97)
    vehicle_detector = ScriptedVehicleDetector({"cam-gate": [vehicle]})
    plate_reader = ScriptedPlateReader(
        {
            id(vehicle): PlateCandidate(
                raw_text=plate_text, char_confidence=(0.95,) * len(plate_text)
            )
        }
    )
    pipeline = DetectionPipeline(
        vehicle_detector, plate_reader, DetectorSettings(motion_gate=False)
    )
    frame = SampledFrame(
        camera_id="cam-gate",
        image=np.zeros((4, 4, 3), dtype=np.uint8),
        timing=FrameTiming(
            pts_ms=0.0,
            delta_ms=None,
            wall_clock=time.time(),
            loop_epoch=0,
            replaying=False,
            discontinuity=True,
        ),
    )
    results = pipeline.process_batch([frame])
    detections = [d for result in results for d in pipeline.to_protobuf(result)]
    assert detections, "the synthetic clip must actually produce a detection"
    return detections


@pytest.fixture
def running_match_engine():
    """The real `MetadataIngestServicer` behind a real gRPC socket, wired to
    the same `RecentAlertsPublisher` the console route reads -- exactly
    `app.py`'s own lifespan wiring (`serve()`, `app.state.recent_alerts`),
    just assembled here instead of via the cached `match_settings()`
    singleton and disk-backed watchlist, so the fixture owns the one
    watchlist entry the gate needs and nothing racing on a shared port or a
    shared lru_cache.
    """
    store = _watchlist_store(KNOWN_PLATE)
    deduper = Deduper(bucket_s=8.0, max_entries=1000)
    recent = RecentAlertsPublisher(max_size=100)
    settings = MatchSettings(grpc_host="127.0.0.1", grpc_port=_free_port())
    grpc_server = serve(store, deduper, recent, settings)
    # `match_app`'s routes read `request.app.state.*` (see `app.py`); a bare
    # `TestClient` never runs the app's own lifespan (confirmed: it only
    # fires inside `with TestClient(app) as client:`), so state set here is
    # the only state the console route sees below.
    match_app.state.recent_alerts = recent
    try:
        yield settings
    finally:
        grpc_server.stop(grace=None)


def test_known_plate_through_a_clip_produces_a_console_alert_within_budget(running_match_engine):
    settings = running_match_engine
    started = time.monotonic()

    detections = _synthetic_clip(KNOWN_PLATE)

    worker_client = MatchEngineClient(
        DetectorSettings(match_engine_grpc=f"{settings.grpc_host}:{settings.grpc_port}")
    )
    try:
        ack = worker_client.send_detections(detections)
    finally:
        worker_client.close()

    assert ack is not None
    assert (ack.accepted, ack.rejected) == (1, 0)

    console = TestClient(match_app)
    response = console.get("/api/v1/alerts")
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    alerts = response.json()
    assert len(alerts) == 1, "one alert, not the raw per-frame detection count (dedup)"

    alert = alerts[0]
    assert alert["matched_entry"]["plate"] == KNOWN_PLATE
    assert alert["explanation"]["matched_plate"] == KNOWN_PLATE
    assert alert["explanation"]["observed_plate"] == KNOWN_PLATE
    assert alert["dedup_key"]

    assert elapsed < GATE_BUDGET_S, (
        f"Day 2 gate budget is {GATE_BUDGET_S}s end to end; took {elapsed:.3f}s"
    )
