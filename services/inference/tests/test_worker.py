"""The worker half of the health contract.

No network and no decoder: a fake registry and a fake capture. What is being
tested is the contract — what the worker reports, what it refuses to assign, and
that a registry outage does not stop ingest.
"""

from __future__ import annotations

import threading
import time

import httpx
import pytest

from prahari_inference.config import IngestSettings
from prahari_inference.worker import (
    CameraAssignment,
    CameraStats,
    IngestWorker,
    RegistryClient,
    _assignment,
)

SETTINGS = IngestSettings(max_active_cameras=3, heartbeat_interval_s=0.01)


class StubCapture:
    """A `StreamCapture` stand-in that never touches a network or a decoder.

    `frames()` blocks on an `Event` rather than returning immediately, so the
    pump thread stays alive exactly like a real one does — until told to stop
    — which is what makes it possible to test "removed" vs "left alone"
    instead of every camera finishing on its own before the assertions run.
    """

    instances: list[StubCapture] = []

    def __init__(self, entry, *, ingest, use_hls=False, url=None):
        self.entry = entry
        self.ingest = ingest
        self.use_hls = use_hls
        self.url = url
        self.closed = False
        self.connected = False
        self.consecutive_failures = 0
        self.last_error: str | None = None
        self._stop = threading.Event()
        StubCapture.instances.append(self)

    def frames(self):
        self._stop.wait()
        if False:  # pragma: no cover - makes this a generator that yields nothing
            yield

    def request_stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self.closed = True
        self.connected = False

    measured_fps = None


class DyingCapture:
    """A `StreamCapture` stand-in whose pump dies instantly: `frames()`
    returns immediately with no frames, exactly like a camera the catalogue
    calls not live, or any other path where the generator returns rather
    than reconnecting forever (I1)."""

    instances: list[DyingCapture] = []

    def __init__(self, entry, *, ingest, use_hls=False, url=None):
        self.closed = False
        self.connected = False
        self.consecutive_failures = 0
        self.last_error: str | None = None
        DyingCapture.instances.append(self)

    def frames(self):
        return iter(())

    def request_stop(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        self.connected = False

    measured_fps = None


class SlowStopCapture:
    """Like `StubCapture`, but the pump takes a moment to notice
    `request_stop()` rather than unblocking the instant it is called --
    modelling the real window between asking a pump to stop and it actually
    exiting, which P7 must not race a second pump into starting during."""

    instances: list[SlowStopCapture] = []

    def __init__(self, entry, *, ingest, use_hls=False, url=None):
        self.closed = False
        self.connected = False
        self.consecutive_failures = 0
        self.last_error: str | None = None
        self._stop = threading.Event()
        SlowStopCapture.instances.append(self)

    def frames(self):
        while not self._stop.is_set():
            time.sleep(0.05)
        if False:  # pragma: no cover - makes this a generator that yields nothing
            yield

    def request_stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self.closed = True
        self.connected = False

    measured_fps = None


def _registry_serving(camera_lists: list[list[dict]], settings: IngestSettings) -> RegistryClient:
    """A `RegistryClient` whose GET (assignments) responses walk through
    `camera_lists` one call at a time, repeating the last entry once
    exhausted. POSTs (heartbeats) always ack healthy."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"camera_id": "x", "state": "healthy", "reason": "ok"})
        idx = min(calls["n"], len(camera_lists) - 1)
        calls["n"] += 1
        return httpx.Response(200, json=camera_lists[idx])

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://registry")
    return RegistryClient(settings, client=http)


def camera(**overrides) -> dict:
    base = {
        "id": "cam-uuid-1",
        "external_id": "101",
        "site_name": "Ashram Road Junction",
        "catalogue_live": True,
        "endpoints": {
            "rtsp_url": "rtsp://gateway.example:8554/stream/101",
            "fanout_rtsp_url": "rtsp://prahari-mediamtx:8554/cam-uuid-1",
        },
    }
    base.update(overrides)
    return base


# --- what gets assigned ------------------------------------------------------


def test_worker_pulls_from_the_fanout_not_the_gateway():
    """One upstream pull per camera. If workers connect to the gateway directly,
    N workers are N connections to a shared government feed."""
    assignment = _assignment(camera())
    assert assignment is not None
    assert assignment.url == "rtsp://prahari-mediamtx:8554/cam-uuid-1"


def test_falls_back_to_the_upstream_url_when_no_fanout_exists():
    """MediaMTX may not be deployed yet — that is how the first connectivity
    test gets done — and one worker against the gateway is within budget."""
    assignment = _assignment(camera(endpoints={"rtsp_url": "rtsp://gw:8554/stream/101"}))
    assert assignment is not None
    assert assignment.url == "rtsp://gw:8554/stream/101"


def test_a_camera_with_no_url_is_skipped_not_guessed():
    """Reconstructing a URL pattern here is the exact mistake the catalogue
    exists to prevent: ids and camera sets change."""
    assert _assignment(camera(endpoints={})) is None


def test_a_camera_the_catalogue_calls_dead_is_not_assigned():
    assert _assignment(camera(catalogue_live=False)) is None


def test_assignment_carries_the_internal_id_not_the_external_one():
    """The heartbeat endpoint is keyed on the registry's own id. External ids
    rotate; a worker that heartbeats against one would 404 after a sync."""
    assignment = _assignment(camera())
    assert assignment.camera_id == "cam-uuid-1"
    assert assignment.external_id == "101"


def test_assignments_are_capped_at_max_active_cameras():
    """A hard cap from the integrator's guide, not a suggestion — exceeding it
    degrades the shared feed for every other consumer on it."""
    cameras = [camera(id=f"cam-{i}") for i in range(10)]
    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=cameras)),
        base_url="http://registry",
    )
    assert len(RegistryClient(SETTINGS, client=http).assignments()) == 3


def test_a_urlless_camera_does_not_consume_a_slot():
    cameras = [camera(id="cam-bad", endpoints={})] + [camera(id=f"cam-{i}") for i in range(3)]
    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=cameras)),
        base_url="http://registry",
    )
    ids = [a.camera_id for a in RegistryClient(SETTINGS, client=http).assignments()]
    assert "cam-bad" not in ids
    assert len(ids) == 3


def test_assignments_are_not_filtered_by_health_state():
    """Every camera starts UNKNOWN and becomes HEALTHY only once a worker has
    reported on it. Asking for healthy cameras assigns nothing on a fresh
    install, and nothing then ever becomes healthy."""
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json=[])

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://registry")
    RegistryClient(SETTINGS, client=http).assignments()
    assert "state" not in seen[0].params


# --- what gets reported ------------------------------------------------------


def test_stats_snapshot_reports_tamper_as_absent_not_false():
    """A camera whose tamper detector has not observed a frame yet must not
    report `false` — that would put "no tampering detected" on a console for a
    detector that has not run. Both fields stay `None` until real."""
    snapshot = CameraStats().snapshot()
    assert snapshot["black_frame_ratio"] is None
    assert snapshot["tamper_suspected"] is None


def test_measured_fps_is_null_until_measured():
    """Null is not a fault. Every camera passes through this state at every
    reconnect, and it must not read as one."""
    assert CameraStats().snapshot()["measured_fps"] is None


def test_the_worker_reports_observations_never_a_verdict():
    """Workers observe; the registry decides. Two workers on the same camera
    must not be able to publish contradictory verdicts."""
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(__import__("json").loads(request.content))
        return httpx.Response(200, json={"camera_id": "cam-1", "state": "healthy", "reason": "ok"})

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://registry")
    worker = IngestWorker(
        [CameraAssignment(camera_id="cam-1", url="rtsp://mtx/cam-1")],
        settings=SETTINGS,
        registry=RegistryClient(SETTINGS, client=http),
    )
    worker._report_once()

    assert len(posted) == 1
    assert "health_state" not in posted[0]
    assert set(posted[0]) >= {"worker_id", "connected", "measured_fps", "frames_decoded"}


def test_a_registry_outage_does_not_stop_ingest():
    """The camera goes stale in the registry's own view, which is correct — from
    the registry's side nothing is arriving — and recovers on the next
    successful report. Crashing the worker would turn a control-plane blip into
    a data-plane outage."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("registry unreachable")

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://registry")
    worker = IngestWorker(
        [CameraAssignment(camera_id="cam-1", url="rtsp://mtx/cam-1")],
        settings=SETTINGS,
        registry=RegistryClient(SETTINGS, client=http),
    )
    worker._report_once()  # must not raise


@pytest.mark.parametrize("status_code", [404, 500])
def test_a_rejected_heartbeat_does_not_stop_the_other_cameras(status_code: int):
    """A camera deleted mid-run 404s. The remaining cameras must still report."""
    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(request.url.path)
        if "cam-gone" in request.url.path:
            return httpx.Response(status_code, json={"detail": "no such camera"})
        return httpx.Response(200, json={"camera_id": "x", "state": "healthy", "reason": "ok"})

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://registry")
    worker = IngestWorker(
        [
            CameraAssignment(camera_id="cam-gone", url="rtsp://mtx/a"),
            CameraAssignment(camera_id="cam-ok", url="rtsp://mtx/b"),
        ],
        settings=SETTINGS,
        registry=RegistryClient(SETTINGS, client=http),
    )
    worker._report_once()

    assert any("cam-ok" in p for p in posted)


def test_worker_never_exceeds_the_camera_cap_even_if_handed_more():
    """Belt and braces: the cap is enforced where the connections are opened,
    not only where the list is fetched."""
    assignments = [CameraAssignment(camera_id=f"cam-{i}", url=f"rtsp://mtx/{i}") for i in range(9)]
    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        base_url="http://registry",
    )
    worker = IngestWorker(
        assignments, settings=SETTINGS, registry=RegistryClient(SETTINGS, client=http)
    )
    assert len(worker._assignments) == 3


# --- HLS threading ------------------------------------------------------------


def test_use_hls_is_threaded_to_the_capture_construction(monkeypatch):
    """`StreamCapture.url` already prefers an explicit override over
    `use_hls`, so the registry's fan-out URL wins either way — this only
    proves the flag actually reaches the constructor, which is the part that
    was missing, not that behaviour changes today."""
    StubCapture.instances.clear()
    monkeypatch.setattr("prahari_inference.worker.StreamCapture", StubCapture)

    settings = IngestSettings(max_active_cameras=3, heartbeat_interval_s=0.01, use_hls=True)
    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        base_url="http://registry",
    )
    worker = IngestWorker(
        [CameraAssignment(camera_id="cam-1", url="rtsp://mtx/cam-1")],
        settings=settings,
        registry=RegistryClient(settings, client=http),
    )
    worker.start()
    worker.stop()

    assert len(StubCapture.instances) == 1
    assert StubCapture.instances[0].use_hls is True
    # And an explicit MediaMTX URL is still what gets passed — use_hls does
    # not somehow suppress or replace it.
    assert StubCapture.instances[0].url == "rtsp://mtx/cam-1"


def test_use_hls_false_is_threaded_through_just_as_faithfully(monkeypatch):
    """The asymmetry is "does the registry URL win", not "is the flag ever
    read" — a worker running with the default must pass `False` explicitly,
    not merely omit the keyword."""
    StubCapture.instances.clear()
    monkeypatch.setattr("prahari_inference.worker.StreamCapture", StubCapture)

    settings = IngestSettings(max_active_cameras=3, heartbeat_interval_s=0.01, use_hls=False)
    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        base_url="http://registry",
    )
    worker = IngestWorker(
        [CameraAssignment(camera_id="cam-1", url="rtsp://mtx/cam-1")],
        settings=settings,
        registry=RegistryClient(settings, client=http),
    )
    worker.start()
    worker.stop()

    assert StubCapture.instances[0].use_hls is False


# --- assignment reconciliation -------------------------------------------------


def test_reconciliation_starts_a_pump_for_a_newly_assigned_camera(monkeypatch):
    """Added → a pump thread, subject to the cap."""
    StubCapture.instances.clear()
    monkeypatch.setattr("prahari_inference.worker.StreamCapture", StubCapture)

    registry = _registry_serving(
        [[camera(id="cam-1")], [camera(id="cam-1"), camera(id="cam-2")]], SETTINGS
    )
    worker = IngestWorker([], settings=SETTINGS, registry=registry)
    try:
        worker._reconcile_assignments()  # first tick: cam-1 appears, pump starts
        worker._reconcile_assignments()  # second tick: cam-2 shows up alongside it

        for camera_id in ("cam-1", "cam-2"):
            assert camera_id in worker._assignments
            assert camera_id in worker._captures
            assert camera_id in worker._pump_threads
            assert worker._pump_threads[camera_id].is_alive()
    finally:
        worker.stop()


def test_reconciliation_stops_a_removed_camera_by_asking_never_by_closing(monkeypatch):
    """Removed → `request_stop()`. Releasing the handle out from under a
    thread blocked in `read()` is an OpenCV segfault, not an exception — the
    pump thread must be the one to call `close()`, in its own `finally`."""
    StubCapture.instances.clear()
    monkeypatch.setattr("prahari_inference.worker.StreamCapture", StubCapture)

    registry = _registry_serving([[camera(id="cam-1")], []], SETTINGS)
    # Starts with nothing assigned — the first reconcile tick is what brings
    # cam-1's pump up, exactly as it would for a worker whose only camera set
    # ever came from reconciliation rather than the constructor.
    worker = IngestWorker([], settings=SETTINGS, registry=registry)
    try:
        worker._reconcile_assignments()  # cam-1 appears, pump starts
        capture = worker._captures["cam-1"]
        worker._reconcile_assignments()  # cam-1 drops out of the registry's list

        assert "cam-1" not in worker._assignments
        assert capture._stop.is_set()  # request_stop() was called
        assert not capture.closed  # close() was NOT called by reconciliation
    finally:
        worker.stop()


def test_reconciliation_leaves_an_unchanged_camera_completely_untouched(monkeypatch):
    """A camera that appears in every fetch must not have its stream
    interrupted because some other camera came or went in the catalogue."""
    StubCapture.instances.clear()
    monkeypatch.setattr("prahari_inference.worker.StreamCapture", StubCapture)

    registry = _registry_serving(
        [[camera(id="cam-1")], [camera(id="cam-1"), camera(id="cam-2")]], SETTINGS
    )
    worker = IngestWorker([], settings=SETTINGS, registry=registry)
    try:
        worker._reconcile_assignments()  # cam-1 appears, pump starts
        original_capture = worker._captures["cam-1"]
        original_thread = worker._pump_threads["cam-1"]

        worker._reconcile_assignments()  # cam-2 is added

        assert worker._captures["cam-1"] is original_capture
        assert worker._pump_threads["cam-1"] is original_thread
        assert not original_capture._stop.is_set()
    finally:
        worker.stop()


def test_reconciliation_caps_additions_at_max_active_cameras():
    """The cap is a hard cap across the whole diff, not just at fetch time —
    exceeding it degrades a shared government feed for every other consumer."""
    settings = IngestSettings(max_active_cameras=2, heartbeat_interval_s=0.01)
    many = [camera(id=f"cam-{i}") for i in range(5)]
    registry = _registry_serving([[], many], settings)
    worker = IngestWorker([], settings=settings, registry=registry)
    try:
        worker._reconcile_assignments()  # nothing yet
        worker._reconcile_assignments()  # 5 candidates, cap is 2

        assert len(worker._assignments) == 2
    finally:
        worker.stop()


def test_reconciliation_reaps_finished_pump_threads(monkeypatch):
    """A removed camera's tracking must eventually disappear, or a long-lived
    worker leaks a thread entry and a stats entry per camera churn."""
    StubCapture.instances.clear()
    monkeypatch.setattr("prahari_inference.worker.StreamCapture", StubCapture)

    registry = _registry_serving([[camera(id="cam-1")], []], SETTINGS)
    worker = IngestWorker([], settings=SETTINGS, registry=registry)
    try:
        worker._reconcile_assignments()  # cam-1 appears, pump starts
        worker._reconcile_assignments()  # drops cam-1, asks its pump to stop
        # The pump thread notices request_stop() and unwinds on its own time;
        # give it a moment, then let the next tick reap it.
        worker._pump_threads["cam-1"].join(timeout=2.0)
        worker._reconcile_assignments()

        assert "cam-1" not in worker._pump_threads
        assert "cam-1" not in worker._captures
        assert "cam-1" not in worker._stats
    finally:
        worker.stop()


def test_a_registry_error_during_reconciliation_leaves_the_running_set_alone(monkeypatch):
    """The safe response to "I don't know what changed" is to change
    nothing — not to tear down cameras that might still be correctly
    assigned."""
    StubCapture.instances.clear()
    monkeypatch.setattr("prahari_inference.worker.StreamCapture", StubCapture)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("registry unreachable")

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://registry")
    worker = IngestWorker(
        [CameraAssignment(camera_id="cam-1", url="rtsp://mtx/cam-1")],
        settings=SETTINGS,
        registry=RegistryClient(SETTINGS, client=http),
    )
    try:
        worker.start()
        worker._reconcile_assignments()  # must not raise, must not touch cam-1

        assert "cam-1" in worker._assignments
        assert worker._pump_threads["cam-1"].is_alive()
    finally:
        worker.stop()


def test_assignment_refresh_disabled_means_no_reconciliation_ever_runs(monkeypatch):
    """A frozen camera set is the reproducible thing to want during a
    measured load run — the switch has to actually disable the refetch, not
    just slow it down."""
    StubCapture.instances.clear()
    monkeypatch.setattr("prahari_inference.worker.StreamCapture", StubCapture)
    called = []
    monkeypatch.setattr(IngestWorker, "_reconcile_assignments", lambda self: called.append(1))

    settings = IngestSettings(
        max_active_cameras=3, heartbeat_interval_s=0.02, assignment_refresh=False
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
    worker.start()
    time.sleep(0.1)  # several heartbeat ticks
    worker.stop()

    assert called == []


# --- I1: a pump that dies while still assigned gets restarted ----------------


def test_a_pump_that_dies_while_still_assigned_gets_restarted(monkeypatch):
    """I1: `frames()` returning -- a camera the catalogue calls not live, or
    any other path where the generator ends without an exception -- must not
    silently strand the camera with no pump forever. It still occupies a
    `max_active_cameras` slot and the registry is never told to reassign it."""
    DyingCapture.instances.clear()
    monkeypatch.setattr("prahari_inference.worker.StreamCapture", DyingCapture)

    settings = IngestSettings(
        max_active_cameras=3, heartbeat_interval_s=0.01, backoff_initial_s=0.01
    )
    # The registry must keep reporting cam-1 as assigned throughout -- this
    # test is about a pump dying while still assigned, not about it being
    # dropped. `_reconcile_assignments` fetches on every call, so the fetch
    # response has to keep including it.
    worker = IngestWorker(
        [CameraAssignment(camera_id="cam-1", url="rtsp://mtx/cam-1")],
        settings=settings,
        registry=_registry_serving([[camera(id="cam-1")]], settings),
    )
    try:
        worker._start_pump(worker._assignments["cam-1"])
        first_thread = worker._pump_threads["cam-1"]
        first_thread.join(timeout=2.0)
        assert not first_thread.is_alive()

        worker._reconcile_assignments()

        assert "cam-1" in worker._assignments
        assert worker._pump_threads["cam-1"] is not first_thread
        assert len(DyingCapture.instances) == 2
    finally:
        worker.stop()


def test_a_repeatedly_dying_pump_is_throttled_by_backoff(monkeypatch):
    """A camera that fails on every attempt must not be restarted on every
    reconciliation tick forever -- the first death restarts immediately, but
    a second death within the backoff window is throttled instead of
    hammering a permanently bad camera."""
    DyingCapture.instances.clear()
    monkeypatch.setattr("prahari_inference.worker.StreamCapture", DyingCapture)

    settings = IngestSettings(
        max_active_cameras=3, heartbeat_interval_s=0.01, backoff_initial_s=10.0
    )
    worker = IngestWorker(
        [CameraAssignment(camera_id="cam-1", url="rtsp://mtx/cam-1")],
        settings=settings,
        registry=_registry_serving([[camera(id="cam-1")]], settings),
    )
    try:
        worker._start_pump(worker._assignments["cam-1"])
        first_thread = worker._pump_threads["cam-1"]
        first_thread.join(timeout=2.0)

        worker._reconcile_assignments()  # first death: restarts immediately
        second_thread = worker._pump_threads["cam-1"]
        assert second_thread is not first_thread
        second_thread.join(timeout=2.0)

        worker._reconcile_assignments()  # second death inside the 10s window: throttled
        assert worker._pump_threads["cam-1"] is second_thread
        assert "cam-1" in worker._assignments
    finally:
        worker.stop()


# --- I2: liveness reflects live pumps, not just a running reporter -----------


def test_touch_liveness_skips_the_file_when_a_pump_is_dead(monkeypatch, tmp_path):
    DyingCapture.instances.clear()
    monkeypatch.setattr("prahari_inference.worker.StreamCapture", DyingCapture)

    liveness_file = tmp_path / "live"
    settings = IngestSettings(
        max_active_cameras=3, heartbeat_interval_s=0.01, liveness_file=str(liveness_file)
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
        worker._start_pump(worker._assignments["cam-1"])
        worker._pump_threads["cam-1"].join(timeout=2.0)

        worker._touch_liveness()

        assert not liveness_file.exists()
    finally:
        worker.stop()


def test_touch_liveness_writes_the_file_when_every_pump_is_alive(monkeypatch, tmp_path):
    StubCapture.instances.clear()
    monkeypatch.setattr("prahari_inference.worker.StreamCapture", StubCapture)

    liveness_file = tmp_path / "live"
    settings = IngestSettings(
        max_active_cameras=3, heartbeat_interval_s=0.01, liveness_file=str(liveness_file)
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
        worker._start_pump(worker._assignments["cam-1"])

        worker._touch_liveness()

        assert liveness_file.exists()
    finally:
        worker.stop()


# --- P7: one upstream pull per camera, even across a fast drop + re-add ------


def test_readding_a_camera_before_its_old_pump_exits_does_not_start_a_second_pump(monkeypatch):
    """P7: a camera dropped from the assignment set and re-added before its
    pump notices `request_stop()` must not get a second pump started while
    the first is still running -- that is two upstream connections to a
    shared government feed. The fresh pump starts once reaping actually
    observes the old one has exited."""
    SlowStopCapture.instances.clear()
    monkeypatch.setattr("prahari_inference.worker.StreamCapture", SlowStopCapture)

    registry = _registry_serving([[camera(id="cam-1")], [], [camera(id="cam-1")]], SETTINGS)
    worker = IngestWorker([], settings=SETTINGS, registry=registry)
    try:
        worker._reconcile_assignments()  # cam-1 appears, pump starts
        original_thread = worker._pump_threads["cam-1"]

        worker._reconcile_assignments()  # cam-1 drops out: request_stop(), not yet noticed
        assert original_thread.is_alive()

        worker._reconcile_assignments()  # cam-1 re-added while the old pump is still stopping
        assert worker._pump_threads["cam-1"] is original_thread
        assert "cam-1" in worker._assignments
        assert len(SlowStopCapture.instances) == 1

        original_thread.join(timeout=2.0)
        assert not original_thread.is_alive()

        worker._reconcile_assignments()  # reaping now sees it died; starts the deferred pump
        assert worker._pump_threads["cam-1"] is not original_thread
        assert len(SlowStopCapture.instances) == 2
    finally:
        worker.stop()


# --- shutdown ----------------------------------------------------------------


def test_stop_asks_captures_to_finish_rather_than_releasing_them():
    """Releasing a VideoCapture while another thread is blocked inside read() is
    a crash in OpenCV, not an exception. The pump thread owns the handle and
    releases it in its own finally; stop() only sets the flag. Every pod takes a
    SIGTERM eventually, so this is an ordinary path, not an edge case."""
    released: list[str] = []
    asked: list[str] = []

    class FakeCapture:
        def request_stop(self):
            asked.append("stop")

        def close(self):
            released.append("close")

    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        base_url="http://registry",
    )
    worker = IngestWorker([], settings=SETTINGS, registry=RegistryClient(SETTINGS, client=http))
    worker._captures["cam-1"] = FakeCapture()
    worker.stop()

    assert asked == ["stop"]
    assert released == []
