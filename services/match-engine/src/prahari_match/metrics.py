"""Minimal plaintext metrics for `GET /metrics` — deliberately no prometheus
client dependency: this service needs a handful of counters and gauges, not a
registry. Rendered in the Prometheus text exposition format so a standard
scraper, or an officer with curl, can read it.

Thread-safe: the gRPC handler threads, the FastAPI threadpool and the uvicorn
event loop all touch these concurrently.

The counters here exist so the failure modes this codebase is built around —
a wedged Redis publish, a Bloom prefilter quietly rejecting everything, dedup
collapsing every alert — show up as a number moving (or not moving) rather
than as silence.
"""

from __future__ import annotations

import threading
from collections import defaultdict

__all__ = [
    "ALERT_PUBLISH_FAILURES",
    "ALERTS_EMITTED",
    "BLOOM_FP_RATE",
    "BLOOM_REJECTED",
    "CANDIDATES_SCORED",
    "DEDUP_SUPPRESSED",
    "DETECTION_PUBLISH_FAILURES",
    "DETECTIONS_ACCEPTED",
    "DETECTIONS_REJECTED",
    "GRPC_ACTIVE_STREAMS",
    "MATCHES_CONFIRMED",
    "MATCHES_PROBABLE",
    "MATCHES_WEAK",
    "METRICS",
    "WATCHLIST_ENTRIES",
    "Metrics",
]

# Counters (monotonic). Suffix `_total` per Prometheus naming convention.
DETECTIONS_ACCEPTED = "prahari_match_detections_accepted_total"
DETECTIONS_REJECTED = "prahari_match_detections_rejected_total"
BLOOM_REJECTED = "prahari_match_bloom_rejected_total"
CANDIDATES_SCORED = "prahari_match_candidates_scored_total"
MATCHES_CONFIRMED = "prahari_match_matches_confirmed_total"
MATCHES_PROBABLE = "prahari_match_matches_probable_total"
MATCHES_WEAK = "prahari_match_matches_weak_total"
ALERTS_EMITTED = "prahari_match_alerts_emitted_total"
DEDUP_SUPPRESSED = "prahari_match_dedup_suppressed_total"
ALERT_PUBLISH_FAILURES = "prahari_match_alert_publish_failures_total"
DETECTION_PUBLISH_FAILURES = "prahari_match_detection_publish_failures_total"

# Gauges (point-in-time).
WATCHLIST_ENTRIES = "prahari_match_watchlist_entries"
BLOOM_FP_RATE = "prahari_match_bloom_false_positive_rate"
GRPC_ACTIVE_STREAMS = "prahari_match_grpc_active_streams"


class Metrics:
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

    def add_gauge(self, name: str, delta: float) -> None:
        """For up/down gauges like in-flight streams, where the caller knows
        the delta, not the absolute value."""
        with self._lock:
            self._gauges[name] = self._gauges.get(name, 0.0) + delta

    def get(self, name: str) -> float:
        """Current value of a counter or gauge; 0 if never touched. For tests
        and for any internal consumer that wants the number, not the text."""
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
