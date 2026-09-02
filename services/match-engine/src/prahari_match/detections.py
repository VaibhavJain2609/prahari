"""Publish every `VehicleDetection` onto the metadata bus, watchlist hit or not.

DAY3-DESIGN.md §2: `grpc_server.py` used to return early on "no plate" and again on "no
watchlist match", which meant a vehicle that was never on the watchlist left no trace
anywhere once its detection message was acked. That is fine for *alerting* and wrong for
*evidence* — the mandatory "trace this plate" test case does not require the plate to be
on the watchlist, and `services/correlation` has nothing to reconstruct a route from
unless every detection, not just every hit, reaches a stream it can read.

Same shape as `alerts.py` deliberately: a `Protocol`, a null implementation for tests and
Redis-less runs, and a lazy-connecting Redis Stream implementation that logs and swallows
rather than propagates — a Redis blip must not fail the gRPC ack a worker is waiting on.
"""

from __future__ import annotations

import logging
from typing import Protocol

from prahari.v1 import events_pb2

__all__ = ["DetectionPublisher", "NullDetectionPublisher", "RedisDetectionPublisher"]

log = logging.getLogger(__name__)


class DetectionPublisher(Protocol):
    def publish(self, detection: events_pb2.VehicleDetection) -> None: ...


class NullDetectionPublisher:
    """The default when no bus is configured, and what tests use so a unit test for
    match/dedup/alert logic does not have to also stand up Redis."""

    def publish(self, detection: events_pb2.VehicleDetection) -> None:  # noqa: ARG002
        return


class RedisDetectionPublisher:
    """Publishes the serialized `VehicleDetection` onto a Redis Stream, capped with
    `MAXLEN ~ maxlen` so an unbounded producer cannot grow the stream forever on a
    long-running demo — this stream carries *every* detection, not just watchlist hits,
    so it is the higher-rate of the two streams this service writes."""

    def __init__(self, redis_url: str, stream_key: str, maxlen: int) -> None:
        self._redis_url = redis_url
        self._stream_key = stream_key
        self._maxlen = maxlen
        self._client = None

    def _client_or_connect(self):
        if self._client is None:
            import redis

            self._client = redis.Redis.from_url(self._redis_url)
        return self._client

    def publish(self, detection: events_pb2.VehicleDetection) -> None:
        try:
            client = self._client_or_connect()
            client.xadd(
                self._stream_key,
                {"detection": detection.SerializeToString()},
                maxlen=self._maxlen,
                approximate=True,
            )
        except Exception:
            log.exception(
                "failed to publish detection %s to redis stream %s",
                detection.detection_id,
                self._stream_key,
            )
