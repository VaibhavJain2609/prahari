"""Minimal plaintext metrics for `GET /metrics` — deliberately no prometheus
client dependency: the registry needs a handful of counters and gauges, not a
registry. Same shape as match-engine's `metrics.py` so one reading habit
covers both services.

Two sources of numbers:

* **Counters** bumped in-process (heartbeats accepted).
* **Gauges refreshed at scrape time** from the database — cameras by effective
  health state, live ingest workers, desired MediaMTX paths. A failed refresh
  serves the last-known values rather than 500ing the scrape: a DB blip is
  exactly when monitoring must keep answering, and a gauge that stops moving
  is itself the signal.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "CAMERAS",
    "HEARTBEATS_RECEIVED",
    "MEDIAMTX_PATHS",
    "METRICS",
    "WORKERS_ALIVE",
    "Metrics",
    "refresh_gauges",
]

# Counters (monotonic). Suffix `_total` per Prometheus naming convention.
HEARTBEATS_RECEIVED = "prahari_registry_heartbeats_received_total"
"""Camera health reports accepted by `POST /cameras/{id}/heartbeat` — the
number that stops moving first when the ingest fleet goes quiet."""

# Gauges (point-in-time, refreshed from Postgres at scrape time).
WORKERS_ALIVE = "prahari_registry_workers_alive"
"""Ingest workers whose assignment lease is still warm."""

CAMERAS = "prahari_registry_cameras"
"""Active cameras, one gauge per effective health state —
`prahari_registry_cameras_healthy`, `_degraded`, … Effective state, not the
raw column: a camera whose heartbeats stopped reads `unreachable` here, which
is the answer 'is anything watching' actually wants."""

MEDIAMTX_PATHS = "prahari_registry_mediamtx_desired_paths"
"""Paths the registry wants reconciled into MediaMTX. Divergence from the
restreamer's own path count is what a failed reconcile looks like."""


class Metrics:
    """Process-local counters and gauges. Thread-safe: the uvicorn event loop
    and any threadpool work touch these concurrently."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: defaultdict[str, float] = defaultdict(float)
        self._gauges: dict[str, float] = {}

    def inc(self, name: str, amount: float = 1.0) -> None:
        with self._lock:
            self._counters[name] += amount

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def get(self, name: str) -> float:
        """Current value of a counter or gauge; 0 if never touched."""
        with self._lock:
            if name in self._counters:
                return self._counters[name]
            return self._gauges.get(name, 0.0)

    def render(self) -> str:
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
        lines: list[str] = []
        for name in sorted(counters):
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {counters[name]:g}")
        for name in sorted(gauges):
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {gauges[name]:g}")
        return "\n".join(lines) + ("\n" if lines else "")


METRICS = Metrics()


async def refresh_gauges(state: Any) -> None:
    """Re-pull the DB-backed gauges onto METRICS. Called at scrape time by
    `/metrics`; `state` is `app.state`, so a test drives this with the same
    fakes the endpoints get.
    """
    try:
        health = await state.repo.health_summary(scope=state.settings.sync_default_org_path)
        for health_state, count in health.items():
            METRICS.set_gauge(f"{CAMERAS}_{health_state}", count)
        METRICS.set_gauge(WORKERS_ALIVE, len(await state.worker_repo.alive_worker_ids()))
        METRICS.set_gauge(MEDIAMTX_PATHS, len(await state.repo.desired_mediamtx_paths()))
    except Exception:
        log.exception("metrics gauge refresh failed; serving last-known values")
