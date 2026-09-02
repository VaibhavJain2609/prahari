"""alerts.py: building the one `Alert` message that rides both gRPC-adjacent
processing and the bus ("one schema, two transports"), and fanning it out
without letting one degraded sink (Redis down) take out the others.
"""

from __future__ import annotations

from prahari.v1 import common_pb2, events_pb2

from prahari_match.alerts import (
    AlertBuilder,
    FanOutPublisher,
    NullPublisher,
    RecentAlertsPublisher,
    RedisStreamPublisher,
)
from prahari_match.matcher import MatchResult


def _detection(camera_id: str = "CAM-1") -> events_pb2.VehicleDetection:
    return events_pb2.VehicleDetection(detection_id="D1", camera_id=camera_id)


def _match_result(reason: int, band: int = common_pb2.CONFIDENCE_BAND_CONFIRMED) -> MatchResult:
    entry = events_pb2.WatchlistEntry(entry_id="E1", plate="GJ01AB1234", reason=reason)
    explanation = events_pb2.MatchExplanation(
        observed_plate="GJ01AB1234", matched_plate="GJ01AB1234", final_score=1.0
    )
    return MatchResult(matched=True, band=band, explanation=explanation, entry=entry)


class TestAlertBuilder:
    def test_builds_alert_from_a_match(self) -> None:
        builder = AlertBuilder()
        result = _match_result(events_pb2.WATCHLIST_REASON_STOLEN)
        alert = builder.build(_detection(), result, dedup_key="CAM-1:GJ01AB1234:125")

        assert alert.alert_id  # non-empty, generated
        assert alert.dedup_key == "CAM-1:GJ01AB1234:125"
        assert alert.matched_entry.plate == "GJ01AB1234"
        assert alert.explanation.matched_plate == "GJ01AB1234"
        assert alert.band == common_pb2.CONFIDENCE_BAND_CONFIRMED
        assert alert.HasField("raised_at")

    def test_two_alerts_get_distinct_ids(self) -> None:
        builder = AlertBuilder()
        result = _match_result(events_pb2.WATCHLIST_REASON_STOLEN)
        a1 = builder.build(_detection(), result, dedup_key="k1")
        a2 = builder.build(_detection(), result, dedup_key="k1")
        assert a1.alert_id != a2.alert_id

    def test_refuses_to_build_from_a_non_match(self) -> None:
        import pytest

        builder = AlertBuilder()
        with pytest.raises(ValueError):
            builder.build(_detection(), MatchResult.no_match(), dedup_key="k1")

    def test_missing_person_outranks_stolen(self) -> None:
        # Priority is not a proxy for band -- a missing-person hit is the
        # highest-stakes reason this system can raise, regardless of score.
        builder = AlertBuilder()
        missing = builder.build(
            _detection(), _match_result(events_pb2.WATCHLIST_REASON_MISSING_PERSON), dedup_key="k"
        )
        stolen = builder.build(
            _detection(), _match_result(events_pb2.WATCHLIST_REASON_STOLEN), dedup_key="k"
        )
        assert missing.priority == events_pb2.ALERT_PRIORITY_CRITICAL
        assert stolen.priority == events_pb2.ALERT_PRIORITY_HIGH
        assert missing.priority > stolen.priority


class TestPublishers:
    def test_null_publisher_accepts_and_discards(self) -> None:
        alert = AlertBuilder().build(
            _detection(), _match_result(events_pb2.WATCHLIST_REASON_STOLEN), dedup_key="k"
        )
        NullPublisher().publish(alert)  # must not raise

    def test_recent_alerts_publisher_returns_newest_first(self) -> None:
        publisher = RecentAlertsPublisher(max_size=10)
        builder = AlertBuilder()
        result = _match_result(events_pb2.WATCHLIST_REASON_STOLEN)
        first = builder.build(_detection(), result, dedup_key="k1")
        second = builder.build(_detection(), result, dedup_key="k2")
        publisher.publish(first)
        publisher.publish(second)
        assert [a.alert_id for a in publisher.recent()] == [second.alert_id, first.alert_id]

    def test_recent_alerts_publisher_is_bounded(self) -> None:
        publisher = RecentAlertsPublisher(max_size=3)
        builder = AlertBuilder()
        result = _match_result(events_pb2.WATCHLIST_REASON_STOLEN)
        for i in range(10):
            publisher.publish(builder.build(_detection(), result, dedup_key=f"k{i}"))
        assert len(publisher.recent()) == 3

    def test_recent_alerts_publisher_respects_limit(self) -> None:
        publisher = RecentAlertsPublisher(max_size=10)
        builder = AlertBuilder()
        result = _match_result(events_pb2.WATCHLIST_REASON_STOLEN)
        for i in range(5):
            publisher.publish(builder.build(_detection(), result, dedup_key=f"k{i}"))
        assert len(publisher.recent(limit=2)) == 2

    def test_redis_publisher_swallows_connection_failure(self) -> None:
        # A Redis outage must never propagate past publish() -- it would fail
        # the gRPC ack a worker is blocked on for a reason that has nothing to
        # do with whether the detection matched.
        publisher = RedisStreamPublisher("redis://127.0.0.1:1", "prahari:alerts")
        alert = AlertBuilder().build(
            _detection(), _match_result(events_pb2.WATCHLIST_REASON_STOLEN), dedup_key="k"
        )
        publisher.publish(alert)  # must not raise even though nothing is listening

    def test_fan_out_publishes_to_every_publisher(self) -> None:
        recent_a = RecentAlertsPublisher(max_size=10)
        recent_b = RecentAlertsPublisher(max_size=10)
        fan_out = FanOutPublisher([recent_a, recent_b])
        alert = AlertBuilder().build(
            _detection(), _match_result(events_pb2.WATCHLIST_REASON_STOLEN), dedup_key="k"
        )
        fan_out.publish(alert)
        assert len(recent_a.recent()) == 1
        assert len(recent_b.recent()) == 1

    def test_fan_out_survives_one_publisher_raising(self) -> None:
        class BrokenPublisher:
            def publish(self, alert: events_pb2.Alert) -> None:
                raise RuntimeError("simulated sink failure")

        recent = RecentAlertsPublisher(max_size=10)
        fan_out = FanOutPublisher([BrokenPublisher(), recent])
        alert = AlertBuilder().build(
            _detection(), _match_result(events_pb2.WATCHLIST_REASON_STOLEN), dedup_key="k"
        )
        fan_out.publish(alert)  # must not raise
        assert len(recent.recent()) == 1  # the working publisher still got it
