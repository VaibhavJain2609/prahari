"""The worker's `/metrics` surface: counters plus read-time gauges, served
over plaintext HTTP by stdlib only — no prometheus dependency, per the
`metrics.py` docstring.
"""

from __future__ import annotations

import urllib.request

import httpx

from prahari_inference.config import IngestSettings
from prahari_inference.metrics import Metrics, MetricsServer
from prahari_inference.worker import CameraAssignment, IngestWorker, RegistryClient

SETTINGS = IngestSettings(max_active_cameras=3, heartbeat_interval_s=0.01)


def _worker(**overrides) -> IngestWorker:
    settings = IngestSettings(max_active_cameras=3, heartbeat_interval_s=0.01, **overrides)
    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        base_url="http://registry",
    )
    return IngestWorker(
        [CameraAssignment(camera_id="cam-1", url="rtsp://mtx/cam-1")],
        settings=settings,
        registry=RegistryClient(settings, client=http),
    )


class TestMetrics:
    def test_render_emits_prahari_worker_prefixed_lines(self):
        metrics = Metrics()
        metrics.inc("batches_flushed")
        metrics.inc("detections_sent", 4)

        body = metrics.render({"streams_active": 2})

        assert "prahari_worker_batches_flushed 1\n" in body
        assert "prahari_worker_detections_sent 4\n" in body
        assert "prahari_worker_streams_active 2\n" in body
        # Counters that never fired still render as 0 — an absent line reads
        # as "not scraped" to a consumer, which is worse than an honest zero.
        assert "prahari_worker_grpc_failures 0\n" in body


class TestMetricsServer:
    def test_metrics_endpoint_serves_the_rendered_body(self):
        metrics = Metrics()
        metrics.inc("grpc_failures", 2)
        server = MetricsServer(lambda: metrics.render({"streams_active": 1}), port=0)
        server.start()
        try:
            body = (
                urllib.request.urlopen(f"http://127.0.0.1:{server.port}/metrics", timeout=5.0)
                .read()
                .decode()
            )
            assert "prahari_worker_grpc_failures 2\n" in body
            assert "prahari_worker_streams_active 1\n" in body
        finally:
            server.close()

    def test_other_paths_404(self):
        server = MetricsServer(lambda: "", port=0)
        server.start()
        try:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{server.port}/", timeout=5.0)
                raise AssertionError("expected a 404")
            except urllib.error.HTTPError as exc:
                assert exc.code == 404
        finally:
            server.close()


class TestWorkerMetricsWiring:
    def test_metrics_port_zero_starts_no_server(self):
        worker = _worker(metrics_port=0)
        try:
            worker.start()
            assert worker._metrics_server is None
        finally:
            worker.stop()

    def test_a_configured_port_serves_live_counters(self):
        worker = _worker()
        # A free ephemeral port: bind, release, hand to the server. The tiny
        # race is inherent to "pick a port for me" on a test machine.
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        settings = IngestSettings(
            max_active_cameras=3, heartbeat_interval_s=0.01, metrics_port=port
        )
        http = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
            base_url="http://registry",
        )
        worker = IngestWorker(
            [CameraAssignment(camera_id="cam-1", url="rtsp://mtx/cam-1")],
            settings=settings,
            registry=RegistryClient(settings, client=http),
        )
        try:
            worker.start()
            body = (
                urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5.0)
                .read()
                .decode()
            )
            assert "prahari_worker_streams_assigned 1\n" in body
            assert "prahari_worker_batches_flushed 0\n" in body
        finally:
            worker.stop()
