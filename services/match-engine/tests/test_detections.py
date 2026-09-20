"""detections.py: the high-rate detections Redis publisher -- socket timeouts
on connect, `MAXLEN ~` on `XADD`, and a publish failure that is logged,
counted and dropped rather than wedging a gRPC handler thread.
"""

from __future__ import annotations

from prahari.v1 import events_pb2

from prahari_match.detections import RedisDetectionPublisher
from prahari_match.metrics import DETECTION_PUBLISH_FAILURES, METRICS


def _detection() -> events_pb2.VehicleDetection:
    return events_pb2.VehicleDetection(detection_id="D1", camera_id="CAM-1")


class TestRedisDetectionPublisher:
    def test_connects_with_socket_timeouts(self, fake_redis) -> None:
        # The point of the hardening: a bare from_url has no socket_timeout,
        # so a hung Redis wedges the calling gRPC handler thread forever.
        publisher = RedisDetectionPublisher(
            "redis://example:6379/0",
            "prahari:detections",
            200_000,
            socket_timeout_s=7.0,
            socket_connect_timeout_s=3.0,
        )
        publisher.publish(_detection())

        assert len(fake_redis.from_url_calls) == 1
        call = fake_redis.from_url_calls[0]
        assert call["url"] == "redis://example:6379/0"
        assert call["socket_timeout"] == 7.0
        assert call["socket_connect_timeout"] == 3.0

    def test_xadd_uses_approximate_maxlen(self, fake_redis) -> None:
        publisher = RedisDetectionPublisher("redis://x", "prahari:detections", 123_456)
        publisher.publish(_detection())

        assert len(fake_redis.xadd_calls) == 1
        call = fake_redis.xadd_calls[0]
        assert call["stream"] == "prahari:detections"
        assert call["maxlen"] == 123_456
        assert call["approximate"] is True
        assert "detection" in call["fields"]

    def test_publish_failure_is_counted_and_dropped(self, fake_redis) -> None:
        # A timed-out publish must not raise (it would fail the worker's ack),
        # must not retry (this stream is too high-rate for that), and must be
        # counted so the drop is visible.
        fake_redis.failures_remaining = 1
        publisher = RedisDetectionPublisher("redis://x", "prahari:detections", 200_000)
        before = METRICS.get(DETECTION_PUBLISH_FAILURES)

        publisher.publish(_detection())

        assert len(fake_redis.xadd_calls) == 1  # no retry on the high-rate stream
        assert METRICS.get(DETECTION_PUBLISH_FAILURES) == before + 1
