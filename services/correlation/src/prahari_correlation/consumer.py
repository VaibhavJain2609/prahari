"""Background thread driving `RedisStreamConsumer` against
`prahari:detections` into the `DetectionStore`. A thread, not an asyncio
task, because `RedisStreamConsumer.poll()` is a blocking `XREAD` -- running
it on the event loop would stall every request this service serves for the
duration of `block_ms` on every poll.
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol

from prahari.v1 import events_pb2
from prahari_common.bus import RedisStreamConsumer

from .store import DetectionStore

__all__ = ["DetectionConsumer"]

log = logging.getLogger(__name__)


class _PingableRedis(Protocol):
    def ping(self) -> bool: ...


class DetectionConsumer:
    def __init__(
        self,
        redis_url: str | None,
        stream_key: str,
        store: DetectionStore,
        *,
        ping_client: _PingableRedis | None = None,
    ) -> None:
        self._redis_url = redis_url
        self._store = store
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ping_client = ping_client
        self._consumer: RedisStreamConsumer[events_pb2.VehicleDetection] | None = None
        if redis_url:
            self._consumer = RedisStreamConsumer(
                redis_url=redis_url,
                stream_key=stream_key,
                field="detection",
                decode=events_pb2.VehicleDetection.FromString,
            )

    def start(self) -> None:
        if self._consumer is None:
            log.warning(
                "PRAHARI_CORRELATION_REDIS_URL not set; detection consumer disabled -- "
                "/api/v1/routes will always return empty routes"
            )
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="detection-consumer")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        assert self._consumer is not None  # only called when start() started the thread
        while not self._stop.is_set():
            for detection in self._consumer.poll():
                try:
                    self._store.add(detection)
                except Exception:
                    # A store bug must not kill the poll thread -- a dead
                    # thread with a still-reachable Redis is exactly the
                    # "looks healthy, silently stopped consuming" failure
                    # `is_connected()` exists to catch, and it can only catch
                    # it if the thread is still here to be checked as alive.
                    log.exception("failed to store detection %s", detection.detection_id)

    def is_connected(self) -> bool:
        """Used by `/readyz` -- a correlation service that silently stopped
        consuming looks healthy and returns empty routes forever, same
        reasoning as match-engine's watchlist-empty check. Checks two
        independent things: the poll thread is still alive (a dead thread
        leaves Redis itself perfectly reachable, so a `PING`-only check would
        report ready forever), and a live `PING` (the URL being set does not
        mean Redis is actually reachable right now)."""
        if self._redis_url is None:
            return False
        if self._thread is not None and not self._thread.is_alive():
            return False
        try:
            client = self._ping_client_or_connect()
            return bool(client.ping())
        except Exception:
            log.exception("detection consumer readiness ping failed")
            return False

    def _ping_client_or_connect(self) -> _PingableRedis:
        if self._ping_client is None:
            import redis

            self._ping_client = redis.Redis.from_url(self._redis_url)
        return self._ping_client
