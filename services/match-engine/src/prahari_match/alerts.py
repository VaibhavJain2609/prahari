"""Build `Alert` protobuf messages and fan them out onto the bus.

"One schema, two transports" (CLAUDE.md): the exact `events_pb2.Alert` built
here is what a gRPC caller gets back embedded in nothing (alerts are not
returned over gRPC -- workers get an `IngestAck`), what lands on Redis
Streams for the bff to relay over SSE, and what `/api/v1/alerts` serves for
local debugging without Redis. One message, built once, in `AlertBuilder`.

Publishing is intentionally forgiving: a Redis outage must not fail the gRPC
ack a worker is waiting on, and must not stop `/api/v1/alerts` from serving
whatever is still in memory. `RedisStreamPublisher` and `FanOutPublisher`
both log and swallow rather than propagate.
"""

from __future__ import annotations

import logging
import uuid
from collections import deque
from collections.abc import Iterable
from typing import Protocol

from google.protobuf.timestamp_pb2 import Timestamp
from prahari.v1 import events_pb2

from .matcher import MatchResult

__all__ = [
    "AlertBuilder",
    "AlertPublisher",
    "FanOutPublisher",
    "NullPublisher",
    "RecentAlertsPublisher",
    "RedisStreamPublisher",
]

log = logging.getLogger(__name__)

# Reason drives priority; band (WEAK/PROBABLE/CONFIRMED) is carried separately
# on the alert so a console can still downrank a CONFIRMED-but-LOW-stakes hit
# without losing the match-quality signal that produced it.
_PRIORITY_BY_REASON: dict[int, int] = {
    events_pb2.WATCHLIST_REASON_MISSING_PERSON: events_pb2.ALERT_PRIORITY_CRITICAL,
    events_pb2.WATCHLIST_REASON_STOLEN: events_pb2.ALERT_PRIORITY_HIGH,
    events_pb2.WATCHLIST_REASON_WANTED: events_pb2.ALERT_PRIORITY_HIGH,
    events_pb2.WATCHLIST_REASON_SUSPECT: events_pb2.ALERT_PRIORITY_MEDIUM,
    events_pb2.WATCHLIST_REASON_BLACKLISTED: events_pb2.ALERT_PRIORITY_MEDIUM,
    events_pb2.WATCHLIST_REASON_UNSPECIFIED: events_pb2.ALERT_PRIORITY_LOW,
}


class AlertBuilder:
    """Turns one confirmed `MatchResult` into one `Alert`. Stateless -- id
    generation is the only side effect, and that is delegated to `uuid4` so
    two builders (e.g. one per gRPC worker thread) never collide."""

    def build(
        self,
        detection: events_pb2.VehicleDetection,
        result: MatchResult,
        dedup_key: str,
    ) -> events_pb2.Alert:
        if not result.matched or result.entry is None or result.explanation is None:
            raise ValueError("cannot build an Alert from a non-match")

        raised_at = Timestamp()
        raised_at.GetCurrentTime()

        priority = _PRIORITY_BY_REASON.get(result.entry.reason, events_pb2.ALERT_PRIORITY_LOW)

        return events_pb2.Alert(
            alert_id=str(uuid.uuid4()),
            raised_at=raised_at,
            priority=priority,
            band=result.band,
            detection=detection,
            matched_entry=result.entry,
            explanation=result.explanation,
            dedup_key=dedup_key,
        )


class AlertPublisher(Protocol):
    def publish(self, alert: events_pb2.Alert) -> None: ...


class NullPublisher:
    """The default when no bus is configured. Alerts are still built and
    scored; they are just not fanned out anywhere -- used by tests and by a
    laptop run before Redis is wired into `make up`."""

    def publish(self, alert: events_pb2.Alert) -> None:  # noqa: ARG002 - Protocol shape
        return


class RedisStreamPublisher:
    """Publishes the serialized `Alert` onto a Redis Stream. Lazily connects
    on first use rather than at construction, so a service can start (and
    serve /healthz) before Redis is reachable -- matching the same
    "observe, don't gate startup on a dependency" pattern as the registry's
    `/healthz`."""

    def __init__(self, redis_url: str, stream_key: str) -> None:
        self._redis_url = redis_url
        self._stream_key = stream_key
        self._client = None

    def _client_or_connect(self):
        if self._client is None:
            import redis

            self._client = redis.Redis.from_url(self._redis_url)
        return self._client

    def publish(self, alert: events_pb2.Alert) -> None:
        try:
            client = self._client_or_connect()
            client.xadd(self._stream_key, {"alert": alert.SerializeToString()})
        except Exception:
            # A Redis blip must not fail the gRPC ack the worker is waiting
            # on, and must not be mistaken for a bad match -- the alert was
            # correctly built and scored; only its delivery to the bus failed.
            log.exception(
                "failed to publish alert %s to redis stream %s", alert.alert_id, self._stream_key
            )


class RecentAlertsPublisher:
    """Bounded in-memory ring buffer backing `/api/v1/alerts`. A debug/admin
    surface, not the system of record -- that is the bus, when configured
    (`MatchSettings.redis_url`)."""

    def __init__(self, max_size: int) -> None:
        self._alerts: deque[events_pb2.Alert] = deque(maxlen=max_size)

    def publish(self, alert: events_pb2.Alert) -> None:
        self._alerts.append(alert)

    def recent(self, limit: int | None = None) -> list[events_pb2.Alert]:
        """Most recent first -- an officer opening the admin view wants the
        latest hit at the top, not the oldest still in the buffer."""
        items = list(reversed(self._alerts))
        return items[:limit] if limit is not None else items


class FanOutPublisher:
    """Publishes to every publisher in the list. One publisher's failure
    (caught and logged, never raised past this point) must not stop the
    others -- otherwise a Redis outage would also silently break the in-memory
    `/api/v1/alerts` surface that is precisely what you want available when
    the bus is degraded."""

    def __init__(self, publishers: Iterable[AlertPublisher]) -> None:
        self._publishers = list(publishers)

    def publish(self, alert: events_pb2.Alert) -> None:
        for publisher in self._publishers:
            try:
                publisher.publish(alert)
            except Exception:
                log.exception(
                    "publisher %s raised while publishing alert %s",
                    type(publisher).__name__,
                    alert.alert_id,
                )
