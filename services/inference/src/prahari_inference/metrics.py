"""A minimal plaintext `/metrics` endpoint for the ingest worker.

Deliberately NOT prometheus_client: the worker needs a handful of counters
visible to `curl` and to an operator reading pod logs, and a dependency —
however small — is not worth that. Lines are emitted in the
`name value` shape Prometheus text exposition uses, so a real scrape config
can parse them the day one exists; nothing here promises the full format
(labels, HELP/TYPE) and nothing needs it yet.

The server is stdlib `http.server` on a daemon thread. A worker's lifecycle is
"kill and reschedule at any moment", so there is no graceful shutdown worth
building — `close()` exists for tests and for `stop()` ordering.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__all__ = ["Metrics", "MetricsServer"]


class Metrics:
    """Process-local integer counters, safe to increment from any thread.

    Pump threads, the flush thread and the reporter thread all write here;
    the render path snapshots under the same lock so a scrape never reads a
    half-incremented set.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {
            "batches_flushed": 0,
            "batch_failures": 0,
            "detections_sent": 0,
            "grpc_failures": 0,
            "heartbeat_failures": 0,
        }

    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counts[name] = self._counts.get(name, 0) + n

    def render(self, gauges: dict[str, int | float]) -> str:
        """`prahari_worker_<name> <value>` lines: counters plus caller-supplied
        gauges (streams active, batch depth) that are computed at read time —
        a gauge stored on write would go stale exactly when it matters."""
        with self._lock:
            values = {**self._counts, **gauges}
        return "".join(f"prahari_worker_{name} {value}\n" for name, value in sorted(values.items()))


class MetricsServer:
    """Serves `GET /metrics` on a daemon thread. Anything else is a 404."""

    def __init__(self, render: Callable[[], str], port: int) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path != "/metrics":
                    self.send_error(404)
                    return
                body = render().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args) -> None:
                pass  # scrapes every few seconds are not worth a log line each

        self._server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="metrics", daemon=True
        )

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
