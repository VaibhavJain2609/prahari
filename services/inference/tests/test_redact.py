"""H1: the fan-out URL's `worker:<worker-media-token>` userinfo is a real
credential and must never reach a pod log or a `last_error` heartbeat field.
The redactor is inference-local on purpose (no registry dependency); these
tests pin both the helper and the two log sites that used to leak it —
`_start_pump`'s "pulling camera" line and `StreamCapture._connect`'s
"connecting camera" line.
"""

from __future__ import annotations

import logging
import threading

import httpx

from prahari_inference import capture as capture_module
from prahari_inference.config import IngestSettings
from prahari_inference.redact import redact_url_credentials, redact_url_in_text
from prahari_inference.worker import CameraAssignment, IngestWorker, RegistryClient

CREDENTIAL_URL = "rtsp://worker:s3cr3t-token@prahari-mediamtx:8554/cam-1"
SETTINGS = IngestSettings(max_active_cameras=3, heartbeat_interval_s=0.01)


def test_redact_strips_userinfo_and_query():
    assert redact_url_credentials(CREDENTIAL_URL) == "rtsp://prahari-mediamtx:8554/cam-1"
    # Credentials in the query string (some DVRs accept ?username=&password=)
    # go too — a log line needs scheme/host/port/path and no more.
    assert (
        redact_url_credentials("rtsp://u:p@dvr:554/ch1?username=a&password=b")
        == "rtsp://dvr:554/ch1"
    )


def test_redact_handles_edge_cases_without_raising():
    assert redact_url_credentials("rtsp://dvr/ch1") == "rtsp://dvr/ch1"
    # IPv6 literals keep their brackets.
    assert redact_url_credentials("rtsp://u:p@[fd00::1]:554/x") == "rtsp://[fd00::1]:554/x"
    # A malformed port degrades to a host-only rendering — a redactor that
    # raises inside a logging call is worse than one that emits less.
    redact_url_credentials("rtsp://u:p@host:bad/x")  # must not raise


def test_redact_url_in_text_replaces_exact_occurrences():
    text = f"RuntimeError: ffmpeg could not open {CREDENTIAL_URL} (timed out)"
    redacted = redact_url_in_text(text, CREDENTIAL_URL)
    assert "s3cr3t-token" not in redacted
    assert "prahari-mediamtx:8554/cam-1" in redacted


class _BlockingCapture:
    """A StreamCapture stand-in that keeps the pump alive until stopped —
    same shape as test_worker.py's StubCapture."""

    def __init__(self, entry, *, ingest, use_hls=False, url=None):
        self.url = url
        self.closed = False
        self.connected = False
        self.consecutive_failures = 0
        self.last_error = None
        self._stop = threading.Event()

    def frames(self):
        self._stop.wait()
        if False:
            yield

    def request_stop(self):
        self._stop.set()

    def close(self):
        self.closed = True

    measured_fps = None


def test_start_pump_logs_the_redacted_url(monkeypatch, caplog):
    """The 'pulling camera' line used to print `assignment.url` raw —
    worker:<token> userinfo straight into pod logs."""
    monkeypatch.setattr("prahari_inference.worker.StreamCapture", _BlockingCapture)
    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        base_url="http://registry",
    )
    worker = IngestWorker(
        [CameraAssignment(camera_id="cam-1", url=CREDENTIAL_URL)],
        settings=SETTINGS,
        registry=RegistryClient(SETTINGS, client=http),
    )
    try:
        caplog.set_level(logging.INFO, logger="prahari_inference.worker")
        worker._start_pump(worker._assignments["cam-1"])
        pulled = [r.getMessage() for r in caplog.records if "pulling camera" in r.getMessage()]
        assert pulled
        assert all("s3cr3t-token" not in m for m in pulled)
        assert "rtsp://prahari-mediamtx:8554/cam-1" in pulled[-1]
    finally:
        worker.stop()


class _OpenedVideoCapture:
    def isOpened(self) -> bool:  # noqa: N802 - cv2's API
        return True

    def release(self) -> None:
        pass


def test_stream_capture_connect_log_is_redacted(monkeypatch, caplog):
    """`_connect` logged `self.url` raw at every connect AND every
    reconnect — the highest-frequency leak site."""
    monkeypatch.setattr(capture_module.cv2, "VideoCapture", lambda *a, **k: _OpenedVideoCapture())
    caplog.set_level(logging.INFO, logger="prahari_inference.capture")
    from prahari_common.catalogue import CameraEntry

    cap = capture_module.StreamCapture(
        CameraEntry(id="cam-1", name="Test", live=True),
        ingest=IngestSettings(),
        url=CREDENTIAL_URL,
    )
    assert cap._connect() is True
    connected = [r.getMessage() for r in caplog.records if "connecting camera" in r.getMessage()]
    assert connected
    assert all("s3cr3t-token" not in m for m in connected)
    assert "rtsp://prahari-mediamtx:8554/cam-1" in connected[-1]
