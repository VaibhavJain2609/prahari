"""grpc_server.py: the servicer that turns a worker's detection stream into
matches, dedup decisions and alerts -- and, per CLAUDE.md, must never
interpret a `HealthEvent` into a health verdict of its own.

Tested by calling the servicer methods directly with a plain Python iterator
standing in for gRPC's `request_iterator` -- `grpc.ServicerContext` is never
touched by the servicer's logic, so a fake stream is enough; no server socket
needed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from google.protobuf.timestamp_pb2 import Timestamp
from prahari.v1 import adapter_pb2, common_pb2, events_pb2

from prahari_match.alerts import AlertBuilder, RecentAlertsPublisher
from prahari_match.bloom import BloomFilter
from prahari_match.config import MatchSettings
from prahari_match.dedup import Deduper
from prahari_match.grpc_server import MetadataIngestServicer
from prahari_match.matcher import WatchlistStore
from prahari_match.watchlist import Watchlist


class _RecordingDetectionPublisher:
    """Records every detection handed to it, in order -- a fake standing in for
    `RedisDetectionPublisher` so these tests never need a real Redis."""

    def __init__(self) -> None:
        self.published: list = []

    def publish(self, detection) -> None:  # noqa: ANN001
        self.published.append(detection)


def _store_with(*plates: str) -> WatchlistStore:
    watchlist = Watchlist()
    for i, plate in enumerate(plates):
        watchlist.add(events_pb2.WatchlistEntry(entry_id=f"E{i}", plate=plate))
    bloom = BloomFilter(expected_items=100)
    for skel in watchlist.skeletons():
        bloom.add(skel)
    return WatchlistStore(watchlist, bloom)


def _detection(
    camera_id: str, plate_text: str, wall_clock_s: float = 1000.0
) -> events_pb2.VehicleDetection:
    ts = Timestamp()
    ts.FromDatetime(datetime.fromtimestamp(wall_clock_s, tz=UTC))
    return events_pb2.VehicleDetection(
        detection_id="D1",
        camera_id=camera_id,
        observed_at=common_pb2.StreamTime(wall_clock=ts),
        plate=events_pb2.PlateReading(
            normalised_text=plate_text, char_confidence=[0.9] * len(plate_text)
        ),
    )


def _servicer(
    store: WatchlistStore,
) -> tuple[MetadataIngestServicer, RecentAlertsPublisher, _RecordingDetectionPublisher]:
    recent = RecentAlertsPublisher(max_size=100)
    deduper = Deduper(bucket_s=8.0, max_entries=1000)
    settings = MatchSettings()
    detections = _RecordingDetectionPublisher()
    servicer = MetadataIngestServicer(
        store, deduper, recent, settings, AlertBuilder(), detection_publisher=detections
    )
    return servicer, recent, detections


class TestStreamDetections:
    def test_matched_detection_produces_one_alert(self) -> None:
        store = _store_with("GJ01AB1234")
        servicer, recent, detections = _servicer(store)

        response = servicer.StreamDetections(
            iter(
                [adapter_pb2.StreamDetectionsRequest(detection=_detection("CAM-1", "GJ01AB1234"))]
            ),
            context=None,
        )

        assert response.ack.accepted == 1
        assert response.ack.rejected == 0
        assert len(recent.recent()) == 1
        assert len(detections.published) == 1  # the raw detection also reaches the bus

    def test_non_matching_detection_is_accepted_but_raises_no_alert(self) -> None:
        # This is the fix DAY3-DESIGN.md §2 exists for: a plate that never hits the
        # watchlist must still reach the detections bus, or services/correlation has
        # nothing to reconstruct a route from for a non-watchlist plate.
        store = _store_with("GJ01AB1234")
        servicer, recent, detections = _servicer(store)

        response = servicer.StreamDetections(
            iter(
                [adapter_pb2.StreamDetectionsRequest(detection=_detection("CAM-1", "MH12ZZ9999"))]
            ),
            context=None,
        )

        assert response.ack.accepted == 1
        assert len(recent.recent()) == 0
        assert len(detections.published) == 1
        assert detections.published[0].plate.normalised_text == "MH12ZZ9999"

    def test_detection_with_no_plate_is_accepted_and_ignored(self) -> None:
        store = _store_with("GJ01AB1234")
        servicer, recent, detections = _servicer(store)

        detection = events_pb2.VehicleDetection(detection_id="D1", camera_id="CAM-1")
        response = servicer.StreamDetections(
            iter([adapter_pb2.StreamDetectionsRequest(detection=detection)]), context=None
        )
        assert response.ack.accepted == 1
        assert len(recent.recent()) == 0
        # No plate to match against, but the sighting itself is still evidence --
        # appearance-only bridging (DAY3-DESIGN.md §3.3) depends on this reaching the bus.
        assert len(detections.published) == 1

    def test_repeat_sighting_in_the_same_bucket_produces_one_alert(self) -> None:
        store = _store_with("GJ01AB1234")
        servicer, recent, detections = _servicer(store)

        requests = [
            adapter_pb2.StreamDetectionsRequest(detection=_detection("CAM-1", "GJ01AB1234", t))
            for t in (1000.0, 1001.0, 1002.0)
        ]
        response = servicer.StreamDetections(iter(requests), context=None)

        assert response.ack.accepted == 3  # every message was processed without error
        assert len(recent.recent()) == 1  # but dedup collapsed it to one alert
        # Dedup governs alerting only -- the detections bus is evidence, not alerting,
        # and gets all three sightings so a route can show dwell time honestly.
        assert len(detections.published) == 3

    def test_detection_with_no_wall_clock_falls_back_to_receipt_time_for_dedup(
        self, monkeypatch
    ) -> None:
        # M6: before the fix, an unset `wall_clock` parsed as the epoch, so
        # EVERY such detection bucketed to floor(0/8)==0 and dedup collapsed
        # to one alert ever -- even sightings hours apart. Two receipt times
        # in different 8s buckets must now still produce two alerts.
        store = _store_with("GJ01AB1234")
        servicer, recent, _detections = _servicer(store)

        # Replace the module-level `time` name grpc_server closes over, not
        # the real `time.time` -- patching the latter globally also breaks
        # the logging module's own internal timestamping.
        class _FakeTime:
            def __init__(self, values: list[float]) -> None:
                self._values = iter(values)

            def time(self) -> float:
                return next(self._values)

        monkeypatch.setattr("prahari_match.grpc_server.time", _FakeTime([1000.0, 1050.0]))

        detection = events_pb2.VehicleDetection(
            detection_id="D1",
            camera_id="CAM-1",
            observed_at=common_pb2.StreamTime(),  # wall_clock deliberately unset
            plate=events_pb2.PlateReading(normalised_text="GJ01AB1234", char_confidence=[0.9] * 10),
        )
        requests = [adapter_pb2.StreamDetectionsRequest(detection=detection) for _ in range(2)]

        response = servicer.StreamDetections(iter(requests), context=None)

        assert response.ack.accepted == 2
        assert response.ack.rejected == 0
        assert len(recent.recent()) == 2

    def test_one_bad_detection_does_not_drop_the_whole_stream(self) -> None:
        # Simulate a transient failure inside the pipeline (e.g. a dedup or
        # publish hiccup) on the first message only, and confirm the stream
        # keeps going and still acks the messages that succeeded.
        store = _store_with("GJ01AB1234")
        recent = RecentAlertsPublisher(max_size=100)
        settings = MatchSettings()

        class FlakyDeduper(Deduper):
            def __init__(self) -> None:
                super().__init__(bucket_s=8.0, max_entries=1000)
                self.calls = 0

            def should_alert(self, camera_id, matched_plate, wall_clock_s=None):  # noqa: ANN001
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("simulated transient failure")
                return super().should_alert(camera_id, matched_plate, wall_clock_s)

        servicer = MetadataIngestServicer(store, FlakyDeduper(), recent, settings, AlertBuilder())

        requests = [
            adapter_pb2.StreamDetectionsRequest(detection=_detection("CAM-1", "GJ01AB1234", t))
            for t in (1000.0, 1009.0)  # second is a new dedup bucket, so it still alerts
        ]
        response = servicer.StreamDetections(iter(requests), context=None)

        assert response.ack.accepted == 1
        assert response.ack.rejected == 1
        assert response.ack.detail  # non-empty, explains the one failure
        assert len(recent.recent()) == 1  # the good message still got through


class TestStreamHealth:
    def test_counts_and_acknowledges_without_computing_a_verdict(self) -> None:
        # CLAUDE.md: "workers observe; the registry decides." This servicer
        # must not derive or expose any health state from these events --
        # there is deliberately no health-state field anywhere in its output.
        store = _store_with("GJ01AB1234")
        servicer, _recent, _detections = _servicer(store)

        events = [
            adapter_pb2.StreamHealthRequest(
                event=events_pb2.HealthEvent(camera_id="CAM-1", observed_fps=5.0)
            )
            for _ in range(4)
        ]
        response = servicer.StreamHealth(iter(events), context=None)

        assert response.ack.accepted == 4
        assert response.ack.rejected == 0
        assert not hasattr(response.ack, "health_state")

    def test_empty_health_stream_is_fine(self) -> None:
        store = _store_with("GJ01AB1234")
        servicer, _recent, _detections = _servicer(store)
        response = servicer.StreamHealth(iter([]), context=None)
        assert response.ack.accepted == 0
