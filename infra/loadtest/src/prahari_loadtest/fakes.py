"""In-process fakes for self-test and local development.

These let the harness verify its own plumbing without Postgres, Redis, or
MediaMTX. They are deliberately thin — `FakeRegistry` stores cameras in a
dict and answers the handful of endpoints the driver touches; it does not
emulate PostGIS scoping, health-verdict derivation, or auth. A green selftest
means "the harness's wiring works", not "the platform is fast".

`FakeIngestServicer` implements `MetadataIngestService` and counts accepted
detections. It does not produce alerts — the e2e alert-latency probe
correctly reports "unmatched" against it, which is the honest answer when no
match engine is behind the socket.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from concurrent import futures
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class _RegistryState:
    def __init__(self) -> None:
        self.cameras: dict[str, dict] = {}
        self.by_external: dict[tuple[str, str], str] = {}
        self.heartbeats: list[dict] = []


def make_registry_handler(state: _RegistryState):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body) -> None:
            payload = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")

        def log_message(self, *args) -> None:  # silence per-request logging
            pass

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)
            if path in ("/healthz", "/readyz"):
                return self._send(200, {"status": "ok", "service": "fake-registry"})
            if path == "/api/v1/cameras":
                lifecycle = qs.get("lifecycle", ["active"])[0]
                limit = int(qs.get("limit", [500])[0])
                offset = int(qs.get("offset", [0])[0])
                cams = [
                    c for c in state.cameras.values() if c.get("lifecycle", "active") == lifecycle
                ]
                return self._send(200, cams[offset : offset + limit])
            if path == "/api/v1/cameras/summary":
                active = sum(
                    1 for c in state.cameras.values() if c.get("lifecycle", "active") == "active"
                )
                return self._send(
                    200,
                    {
                        "active": active,
                        "absent": 0,
                        "decommissioned": sum(
                            1
                            for c in state.cameras.values()
                            if c.get("lifecycle") == "decommissioned"
                        ),
                        "health": {"healthy": active},
                    },
                )
            if path == "/api/v1/cameras/geojson":
                features = [
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [
                                c["location"]["longitude"],
                                c["location"]["latitude"],
                            ],
                        },
                        "properties": {"id": c["id"], "state": "healthy"},
                    }
                    for c in state.cameras.values()
                    if c.get("location") and c.get("lifecycle", "active") == "active"
                ]
                return self._send(200, {"type": "FeatureCollection", "features": features})
            return self._send(404, {"detail": "fake-registry: no such route"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            body = self._body()
            if path == "/api/v1/cameras":
                key = (body.get("source", "manual"), body["external_id"])
                if key in state.by_external:
                    return self._send(409, {"detail": f"{key[0]}/{key[1]} already registered"})
                cam = dict(body)
                cam["id"] = str(uuid.uuid4())
                cam["lifecycle"] = "active"
                cam["catalogue_live"] = True
                cam["endpoints"] = {}
                cam["created_at"] = _now()
                state.cameras[cam["id"]] = cam
                state.by_external[key] = cam["id"]
                return self._send(201, cam)
            if path == "/api/v1/streams/reconcile":
                return self._send(200, {"added": 0, "updated": 0, "removed": 0, "failed": 0})
            # /api/v1/cameras/{id}/heartbeat
            parts = path.strip("/").split("/")
            heartbeat = len(parts) == 5 and parts[4] == "heartbeat"
            if heartbeat and parts[:3] == ["api", "v1", "cameras"]:
                cam_id = parts[3]
                if cam_id not in state.cameras:
                    return self._send(404, {"detail": f"no camera {cam_id}"})
                state.heartbeats.append({"camera_id": cam_id, **body})
                return self._send(
                    200,
                    {
                        "camera_id": cam_id,
                        "state": "healthy",
                        "reason": "fake-registry accepts all",
                        "baseline_fps": body.get("measured_fps"),
                    },
                )
            return self._send(404, {"detail": "fake-registry: no such route"})

        def do_DELETE(self) -> None:  # noqa: N802
            parts = urlparse(self.path).path.strip("/").split("/")
            if len(parts) == 4 and parts[:3] == ["api", "v1", "cameras"]:
                cam = state.cameras.get(parts[3])
                if cam is None:
                    return self._send(404, {"detail": f"no camera {parts[3]}"})
                cam["lifecycle"] = "decommissioned"
                return self._send(200, cam)
            return self._send(404, {"detail": "fake-registry: no such route"})

    return Handler


class FakeRegistry:
    """A registry stand-in on an ephemeral localhost port."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.state = _RegistryState()
        self._server = ThreadingHTTPServer((host, port), make_registry_handler(self.state))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FakeRegistry:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> FakeRegistry:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def make_fake_ingest_server(grpc_target_port: int = 0):
    """A `MetadataIngestService` that accepts and counts. Returns (server, port, counters).

    Raises RuntimeError when the generated stubs are absent — the caller
    reports the gRPC leg as skipped, not failed.
    """
    from .detections import GRPC_IMPORT_ERROR

    if GRPC_IMPORT_ERROR:
        raise RuntimeError(f"proto stubs unavailable: {GRPC_IMPORT_ERROR}")

    import grpc
    from prahari.v1 import adapter_pb2, adapter_pb2_grpc

    counters = {"accepted": 0, "rejected": 0}

    class Servicer(adapter_pb2_grpc.MetadataIngestServiceServicer):
        def StreamDetections(self, request_iterator, context):
            # Ack reports THIS stream's counts — same contract as the real
            # servicer — while `counters` accumulates globally for the
            # selftest's assertion.
            n = 0
            for _req in request_iterator:
                n += 1
                counters["accepted"] += 1
            return adapter_pb2.StreamDetectionsResponse(
                ack=adapter_pb2.IngestAck(accepted=n, rejected=0, detail="")
            )

        def StreamHealth(self, request_iterator, context):
            n = sum(1 for _ in request_iterator)
            return adapter_pb2.StreamHealthResponse(
                ack=adapter_pb2.IngestAck(accepted=n, rejected=0, detail="")
            )

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    adapter_pb2_grpc.add_MetadataIngestServiceServicer_to_server(Servicer(), server)
    port = server.add_insecure_port(f"127.0.0.1:{grpc_target_port}")
    server.start()
    return server, port, counters
