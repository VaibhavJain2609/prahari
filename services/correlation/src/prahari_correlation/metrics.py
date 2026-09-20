"""Hand-rolled plaintext metrics for `GET /metrics` -- no prometheus client
dependency, matching the rest of this repo's "stdlib unless it earns its
place" posture. One `Metrics` instance is created in the app lifespan and
handed to the store, consumer and registry client, each of which increments
the counters it owns; gauges are registered as zero-arg callables and sampled
at render time.

Thread-safe: `inc()` runs inside `DetectionStore.add`, which the detection
consumer task drives while `render()` runs on request handlers — same event
loop today, but the lock keeps the class correct for any caller.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

__all__ = ["Metrics"]

_PREFIX = "prahari_correlation_"


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = {}
        # Gauges are callables evaluated at render time -- the metrics reader
        # asks the store/consumer "how many right now" rather than trusting a
        # value someone remembered to keep in sync.
        self._gauges: dict[str, Callable[[], float | None]] = {}

    def inc(self, name: str, amount: float = 1.0) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0.0) + amount

    def gauge(self, name: str, fn: Callable[[], float | None]) -> None:
        """Register a gauge. `fn` returning `None` means "unknown right now"
        (e.g. consumer lag with no Redis configured) -- the line is omitted
        rather than rendered as a misleading 0."""
        self._gauges[name] = fn

    def render(self) -> str:
        """Prometheus text exposition, minimal flavour: one `name value` line
        per series, no HELP/TYPE headers -- the endpoint exists for the demo
        dashboard and `curl`, not for a real scraper's parser."""
        with self._lock:
            counters = dict(self._counters)
        lines = [f"{_PREFIX}{name} {value:g}" for name, value in sorted(counters.items())]
        for name, fn in sorted(self._gauges.items()):
            try:
                value = fn()
            except Exception:
                # A gauge whose source is broken must not take /metrics down
                # with it -- omitting the line is the honest answer.
                continue
            if value is not None:
                lines.append(f"{_PREFIX}{name} {value:g}")
        lines.append("")  # trailing newline
        return "\n".join(lines)
