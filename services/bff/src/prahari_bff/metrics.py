"""Minimal plaintext metrics for `GET /metrics` — deliberately no prometheus
client dependency: the BFF needs a handful of counters and gauges, not a
registry. Same shape as the registry's and match-engine's `metrics.py` so one
reading habit covers all three.

Three sources of numbers:

* **Counters** bumped in-process (HTTP responses by status class — the
  always-on middleware counts them, which is also what makes this endpoint
  honest about its own scrapes).
* **Gauges refreshed at scrape time** — live SSE connections from app.state,
  and the audit chain's row count. The audit gauge doubles as the truncation
  tripwire the `audit/head` endpoint serves: a count that stops moving while
  traffic continues is exactly what a tampered or wedged audit store looks
  like. A failed refresh serves last-known values rather than 500ing the
  scrape — a wedged audit DB is exactly when monitoring must keep answering.

The BFF's port is browser-reachable (it is the console's upstream), so this
endpoint exposes operational counters only — no paths, no actors, no query
strings.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "AUDIT_ENTRIES",
    "HTTP_REQUESTS",
    "METRICS",
    "SSE_ACTIVE",
    "Metrics",
    "count_response",
    "refresh_gauges",
]

# Counters (monotonic). One metric per status class — `2xx`/`4xx`/`5xx` —
# rendered as `prahari_bff_http_requests_4xx_total` etc. Status classes only:
# path labels would be unbounded cardinality and would leak route params.
HTTP_REQUESTS = "prahari_bff_http_requests"

# Gauges (point-in-time).
SSE_ACTIVE = "prahari_bff_sse_active_connections"
"""Live alert-stream subscribers. The cap (`sse_max_connections`) is the
denominator this gauge is read against — a gauge pinned at the cap is the
'users see no live alerts' symptom."""

AUDIT_ENTRIES = "prahari_bff_audit_entries"
"""Rows in the hash-chained audit log. Monotonic by invariant — a value that
moves backwards is tail truncation, which `audit/head` exists to prove."""


class Metrics:
    """Process-local counters and gauges. Thread-safe: the uvicorn event
    loop and any threadpool work touch these concurrently."""

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


def count_response(status_code: int) -> None:
    """Bucket one completed response by status class. Called from the
    always-on middleware so every served response — including `/metrics`
    itself — is counted."""
    if status_code >= 500:
        klass = "5xx"
    elif status_code >= 400:
        klass = "4xx"
    else:
        klass = "2xx"
    METRICS.inc(f"{HTTP_REQUESTS}_{klass}_total")


async def refresh_gauges(state: Any) -> None:
    """Re-pull the state-backed gauges onto METRICS at scrape time; `state`
    is `app.state`, so a test drives this with the same fakes the endpoints
    get. Best-effort: last-known values on failure."""
    try:
        METRICS.set_gauge(SSE_ACTIVE, float(getattr(state, "sse_active", 0)))
        audit = getattr(state, "audit", None)
        if audit is not None:
            _head_hash, row_count = await audit.head()
            METRICS.set_gauge(AUDIT_ENTRIES, float(row_count))
    except Exception:
        log.exception("metrics gauge refresh failed; serving last-known values")
