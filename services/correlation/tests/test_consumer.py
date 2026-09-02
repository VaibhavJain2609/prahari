from __future__ import annotations

import threading

from prahari_correlation.consumer import DetectionConsumer
from prahari_correlation.store import DetectionStore


def test_no_redis_url_means_never_connected() -> None:
    consumer = DetectionConsumer(None, "prahari:detections", DetectionStore(10, 10))
    assert not consumer.is_connected()


def test_start_with_no_redis_url_does_not_spawn_a_thread() -> None:
    consumer = DetectionConsumer(None, "prahari:detections", DetectionStore(10, 10))
    consumer.start()
    assert consumer._thread is None  # noqa: SLF001 -- the only externally-checkable proof


def test_is_connected_reflects_a_successful_ping() -> None:
    class _OkPing:
        def ping(self) -> bool:
            return True

    consumer = DetectionConsumer(
        "redis://irrelevant", "prahari:detections", DetectionStore(10, 10), ping_client=_OkPing()
    )
    assert consumer.is_connected()


def test_is_connected_is_false_when_ping_raises() -> None:
    class _BrokenPing:
        def ping(self) -> bool:
            raise ConnectionError("simulated redis outage")

    consumer = DetectionConsumer(
        "redis://irrelevant",
        "prahari:detections",
        DetectionStore(10, 10),
        ping_client=_BrokenPing(),
    )
    assert not consumer.is_connected()


def test_is_connected_is_false_when_the_poll_thread_has_died() -> None:
    # Redis itself is perfectly reachable here (ping succeeds) -- this is
    # the "poll thread died, Redis did not" case /readyz exists to catch,
    # which a PING-only check would silently miss.
    class _OkPing:
        def ping(self) -> bool:
            return True

    consumer = DetectionConsumer(
        "redis://irrelevant", "prahari:detections", DetectionStore(10, 10), ping_client=_OkPing()
    )
    dead_thread = threading.Thread(target=lambda: None)
    dead_thread.start()
    dead_thread.join()
    consumer._thread = dead_thread  # noqa: SLF001 -- simulating a poll thread that has exited

    assert not consumer.is_connected()


def test_stop_before_start_does_not_raise() -> None:
    consumer = DetectionConsumer(None, "prahari:detections", DetectionStore(10, 10))
    consumer.stop()  # must be a no-op, not an AttributeError on a never-started thread
