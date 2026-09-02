"""The ingest worker: pull assigned cameras, report health to the registry.

Day 1 scope. Detection, OCR and the gRPC link to the match engine land on Day 2;
what runs here now is the half that has to be right before any of that is worth
building — cameras get pulled, and the registry learns whether they are actually
delivering.

Two rules shape the whole module:

* **Workers observe; the registry decides.** Nothing here computes a health
  state. It reports connected/not, a measured rate, decoded counts and the last
  error, and the registry turns that into HEALTHY / DEGRADED / UNREACHABLE. Two
  workers on the same camera must not be able to publish contradictory verdicts,
  and a worker cannot see that its own heartbeats have stopped arriving.

* **The registry says which cameras and from where.** No URL is reconstructed
  here. The assignment carries the MediaMTX fan-out URL, because every client
  that connects to a source gets its own copy of the stream and the government
  gateway must see exactly one pull per camera.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from prahari_common.catalogue import CameraEntry

from .capture import SampleGate, StreamCapture
from .config import DetectorSettings, IngestSettings, detector_settings, ingest_settings
from .detect import (
    CrossCameraBatcher,
    DetectionPipeline,
    PaddlePlateReader,
    SampledFrame,
    TamperDetector,
    YoloVehicleDetector,
)
from .grpc_client import MatchEngineClient

log = logging.getLogger(__name__)


def worker_id() -> str:
    """Stable within a pod lifetime, unique across pods.

    The pod name under Kubernetes, the hostname otherwise. It is written on every
    heartbeat so that "which worker said this camera was down" is answerable
    without correlating timestamps across pod logs.
    """
    return os.getenv("HOSTNAME") or socket.gethostname()


@dataclass
class CameraAssignment:
    """One camera this worker is responsible for, as the registry describes it."""

    camera_id: str
    """The registry's internal id — the one the heartbeat endpoint is keyed on.
    Not the catalogue's external id, which rotates."""

    url: str
    site_name: str | None = None
    external_id: str | None = None
    live: bool = True


@dataclass
class CameraStats:
    """What one capture thread has observed. Read by the reporter thread.

    Plain attributes under a lock rather than a queue: the reporter wants the
    *current* state, not every intermediate one, and a queue would either grow
    without bound or drop the newest reading.
    """

    connected: bool = False
    measured_fps: float | None = None
    frames_decoded: int = 0
    last_frame_at: datetime | None = None
    consecutive_failures: int = 0
    loop_epoch: int = 0
    last_error: str | None = None
    black_frame_ratio: float | None = None
    """None until the tamper detector has observed at least one sampled frame
    on this camera — never coerced to 0.0 for "not observed", which a console
    would be unable to tell apart from "observed and confirmed clear"."""
    tamper_suspected: bool | None = None
    """Same rule as `black_frame_ratio`. Reported as absent rather than False
    so a console cannot show "no tampering detected" for a detector that has
    not run; only real values once it has."""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "connected": self.connected,
                "measured_fps": self.measured_fps,
                "frames_decoded": self.frames_decoded,
                "last_frame_at": (self.last_frame_at.isoformat() if self.last_frame_at else None),
                "consecutive_failures": self.consecutive_failures,
                "loop_epoch": self.loop_epoch,
                "last_error": self.last_error,
                "black_frame_ratio": self.black_frame_ratio,
                "tamper_suspected": self.tamper_suspected,
            }

    def update(self, **fields) -> None:
        with self._lock:
            for k, v in fields.items():
                setattr(self, k, v)


class RegistryClient:
    """The worker's half of the health contract."""

    def __init__(self, settings: IngestSettings, *, client: httpx.Client | None = None) -> None:
        self._s = settings
        self._http = client or httpx.Client(base_url=settings.registry_url, timeout=10.0)

    def assignments(self) -> list[CameraAssignment]:
        """Ask the registry what to pull.

        `max_active_cameras` is a hard cap from the integrator's guide, not a
        suggestion: exceeding it degrades the shared feed for every other
        consumer on it. It is applied here as well as server-side because the
        consequence of getting it wrong is borne by someone else.

        Deliberately not filtered by health state. Every camera starts UNKNOWN
        and only becomes HEALTHY once a worker has reported on it, so asking for
        healthy cameras would assign nothing on a fresh install and nothing would
        ever become healthy. The catalogue's own live flag is the right filter —
        §5: confirm live status in /api/ingest before reporting a camera down —
        and it is applied by `_assignment`, not here, so that a camera the
        catalogue calls dead is still visible on the map with a reason.
        """
        response = self._http.get(
            "/api/v1/cameras",
            params={"lifecycle": "active", "limit": self._s.max_active_cameras},
        )
        response.raise_for_status()
        return [a for c in response.json() if (a := _assignment(c)) is not None][
            : self._s.max_active_cameras
        ]

    def heartbeat(self, camera_id: str, payload: dict) -> None:
        response = self._http.post(f"/api/v1/cameras/{camera_id}/heartbeat", json=payload)
        response.raise_for_status()
        ack = response.json()
        if ack.get("state") != "healthy":
            # Logged at the worker too: an operator watching pod logs during a
            # demo should not have to open the console to see why a camera went
            # amber.
            log.warning(
                "camera=%s registry verdict %s: %s (baseline %.2f fps)",
                camera_id,
                ack.get("state"),
                ack.get("reason"),
                ack.get("baseline_fps") or 0.0,
            )

    def close(self) -> None:
        self._http.close()


def _assignment(camera: dict) -> CameraAssignment | None:
    """Turn one registry `Camera` into an assignment, or skip it.

    A camera with no reachable URL is skipped rather than defaulted: guessing a
    URL pattern here is exactly the mistake the catalogue exists to prevent.
    """
    if not camera.get("catalogue_live", True):
        # Opening a capture against a camera the catalogue reports as down burns
        # a connection slot on a feed that cannot deliver. StreamCapture refuses
        # too; skipping here means the slot goes to a camera that can use it.
        return None

    endpoints = camera.get("endpoints") or {}
    url = (
        endpoints.get("fanout_rtsp_url")
        or endpoints.get("fanout_hls_url")
        or endpoints.get("rtsp_url")
        or endpoints.get("hls_url")
    )
    if not url:
        log.warning("camera=%s has no stream URL; not assigning", camera.get("id"))
        return None
    return CameraAssignment(
        camera_id=camera["id"],
        url=url,
        site_name=camera.get("site_name"),
        external_id=camera.get("external_id"),
        live=camera.get("catalogue_live", True),
    )


class IngestWorker:
    """Pulls a set of cameras and reports their health until told to stop."""

    def __init__(
        self,
        assignments: list[CameraAssignment],
        *,
        settings: IngestSettings | None = None,
        detect_settings: DetectorSettings | None = None,
        registry: RegistryClient | None = None,
        pipeline: DetectionPipeline | None = None,
        match_client: MatchEngineClient | None = None,
    ) -> None:
        self._s = settings or ingest_settings()
        self._ds = detect_settings or detector_settings()
        self._registry = registry or RegistryClient(self._s)
        self._pipeline = pipeline or DetectionPipeline(
            YoloVehicleDetector(self._ds), PaddlePlateReader(self._ds), self._ds
        )
        self._match_client = match_client or MatchEngineClient(self._ds)
        # Shared across every camera's pump thread, matching CLAUDE.md's
        # "batch across cameras, never within one": a single camera at
        # sample_fps=2 cannot fill a batch without adding real latency, while
        # a handful of active cameras fill one in tens of milliseconds.
        self._batcher = CrossCameraBatcher(self._handle_batch, self._ds)
        capped = assignments[: self._s.max_active_cameras]
        # Keyed by camera_id rather than a list: reconciliation needs to diff
        # the running set against a fresh fetch, add and remove individual
        # cameras, and leave everything else untouched. A list makes "leave
        # everything else untouched" an exercise in bookkeeping; a dict makes
        # it the default.
        self._assignments: dict[str, CameraAssignment] = {a.camera_id: a for a in capped}
        self._stats: dict[str, CameraStats] = {cid: CameraStats() for cid in self._assignments}
        self._captures: dict[str, StreamCapture] = {}
        self._pump_threads: dict[str, threading.Thread] = {}
        # I1: per-camera restart backoff for a pump that dies while still
        # assigned. `_pump_retry_at` is when the next restart may happen;
        # `_pump_backoff` is the delay that restart will apply before the one
        # after it. Mirrors `StreamCapture`'s own reconnect backoff, one level
        # up -- without it, a camera whose pump dies instantly on every
        # attempt (a malformed frame that reliably crashes the same way) would
        # be restarted every reconciliation tick forever.
        self._pump_backoff: dict[str, float] = {}
        self._pump_retry_at: dict[str, float] = {}
        self._reporter_thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Guards `_assignments`, `_captures`, `_pump_threads` and `_stats` as
        # collections (membership, not the fields inside one CameraStats,
        # which has its own lock). Reconciliation runs on the reporter thread;
        # `stop()` and test code can run on any other thread, and a camera
        # being added or reaped while either iterates these dicts is a
        # `RuntimeError: dictionary changed size during iteration`, not a
        # theoretical concern.
        self._lock = threading.Lock()

    # --- one camera ----------------------------------------------------------

    def _start_pump(self, assignment: CameraAssignment) -> None:
        """Build the capture and launch its pump thread.

        The capture is constructed here — on the thread doing the starting or
        reconciling — and handed to the pump thread rather than built inside
        it. That way `self._captures[camera_id]` exists before the thread
        does, so `stop()` (or a reconcile that races a fresh start) can never
        observe a pump thread with no capture yet to ask to stop.

        Caller must hold `self._lock` if called from reconciliation; `start()`
        calls it before the reporter thread exists, so nothing else can be
        touching these dicts yet.
        """
        entry = CameraEntry(
            id=assignment.external_id or assignment.camera_id,
            name=assignment.site_name,
            live=assignment.live,
        )
        capture = StreamCapture(
            entry,
            ingest=self._s,
            # The registry always hands us an explicit fan-out (or, absent
            # one, direct-gateway) URL, and `StreamCapture.url` checks that
            # override before it ever looks at `use_hls`. So this has no
            # effect today — but it is threaded through anyway, so this call
            # site stays correct if that ever stops being true, rather than
            # correct only by accident of what the registry currently sends.
            use_hls=self._s.use_hls,
            url=assignment.url,
        )
        self._captures[assignment.camera_id] = capture
        log.info(
            "pulling camera=%s (%s) from %s",
            assignment.camera_id,
            assignment.site_name or "unnamed",
            assignment.url,
        )
        thread = threading.Thread(
            target=self._pump, args=(assignment, capture), name=f"pump-{assignment.camera_id[:8]}"
        )
        thread.daemon = True
        self._pump_threads[assignment.camera_id] = thread
        thread.start()

    def _pump(self, assignment: CameraAssignment, capture: StreamCapture) -> None:
        stats = self._stats[assignment.camera_id]
        sample_gate = SampleGate(self._s.sample_fps)
        tamper = TamperDetector()

        try:
            for frame in capture.frames():
                if self._stop.is_set():
                    break
                # I3/I4: `connected`, `consecutive_failures` and `last_error`
                # are NOT set here. `_report_once` reads them straight off
                # `capture` at heartbeat time instead -- this loop only runs
                # while frames are arriving, and a stalled reconnect (up to
                # 30s per attempt, indefinitely) means this body simply does
                # not execute for as long as the stall lasts. A value only
                # ever written here would report the state as of the last
                # frame, however old that gets.
                stats.update(
                    frames_decoded=stats.frames_decoded + 1,
                    last_frame_at=datetime.now(UTC),
                    measured_fps=capture.measured_fps,
                    loop_epoch=frame.timing.loop_epoch,
                )
                # Tamper runs on the sampled stream, same as detection: at
                # full decode rate its own cost would rival the detector's,
                # for a signal that does not need every frame to be useful.
                if sample_gate.should_process(frame.timing):
                    sampled = SampledFrame(
                        camera_id=assignment.camera_id,
                        image=frame.image,
                        timing=frame.timing,
                    )
                    report = tamper.observe(sampled)
                    stats.update(
                        black_frame_ratio=report.black_frame_ratio,
                        tamper_suspected=report.suspected,
                    )
                    self._batcher.submit(sampled)
        except Exception as exc:
            # The capture's own reconnect loop handles connection loss; reaching
            # here means something else broke. Record it so the camera shows as
            # unreachable with a reason rather than silently going quiet.
            log.exception("camera=%s pump failed", assignment.camera_id)
            stats.update(connected=False, last_error=f"{type(exc).__name__}: {exc}")
        finally:
            # Close the capture: we hold a connection slot on a shared feed for
            # exactly as long as we are processing the camera, and no longer.
            capture.close()
            stats.update(connected=False)

    def _handle_batch(self, frames: list[SampledFrame]) -> None:
        """Run one cross-camera batch through the detection cascade and
        publish whatever it produced.

        Called by `CrossCameraBatcher` — on whichever pump thread's `submit()`
        filled the batch, or on the batcher's own flush thread when the
        deadline fires first. The cascade always runs, even with publishing
        off: `DetectorSettings.publish_enabled` exists for offline throughput
        measurement, where the point is to profile the cascade itself, not to
        skip it.
        """
        results = self._pipeline.process_batch(frames)
        if not self._ds.publish_enabled:
            return
        detections = [d for result in results for d in self._pipeline.to_protobuf(result)]
        if detections:
            self._match_client.send_detections(detections)

    # --- assignment reconciliation --------------------------------------------

    def _reconcile_assignments(self) -> None:
        """Re-fetch assignments and diff against the running set.

        Runs on the reporter thread's own tick — there is no separate timer.
        A camera the catalogue sync adds mid-run is invisible until this
        fires; a camera dropped from the assignment list stops here without
        disturbing any camera that is still listed.
        """
        if self._stop.is_set():
            return
        try:
            fresh = self._registry.assignments()
        except httpx.HTTPError as exc:
            # An unreachable registry must not stop ingest, and must not tear
            # down cameras we cannot currently confirm — the safe response to
            # "I don't know what changed" is to change nothing.
            log.warning("assignment refresh failed, keeping current set: %s", exc)
            return

        fresh_by_id = {a.camera_id: a for a in fresh}

        with self._lock:
            if self._stop.is_set():
                return

            removed_ids = [cid for cid in self._assignments if cid not in fresh_by_id]
            for camera_id in removed_ids:
                # Ask, never release: the pump thread owns its capture handle
                # and releases it in its own `finally`. Tearing it out from
                # under a thread blocked in `read()` is an OpenCV segfault,
                # not an exception.
                capture = self._captures.get(camera_id)
                if capture is not None:
                    capture.request_stop()
                del self._assignments[camera_id]
                log.info("camera=%s dropped from assignment; requested stop", camera_id)

            # The cap applies across the whole diff, removals included:
            # exceeding it degrades a shared government feed for every other
            # consumer, not only this pod.
            added_ids = [cid for cid in fresh_by_id if cid not in self._assignments]
            available = max(self._s.max_active_cameras - len(self._assignments), 0)
            for camera_id in added_ids[:available]:
                assignment = fresh_by_id[camera_id]
                self._assignments[camera_id] = assignment

                old_thread = self._pump_threads.get(camera_id)
                if old_thread is not None and old_thread.is_alive():
                    # P7: this camera was removed and re-added before its
                    # previous pump noticed `request_stop()`. Starting a
                    # second one now means two upstream pulls on the same
                    # camera -- on a shared government feed, that is the
                    # invariant with a cost outside this pod. The assignment
                    # is recorded; `_reap_and_restart_pumps` starts the fresh
                    # pump itself, the moment the old thread actually exits.
                    log.info(
                        "camera=%s re-added while its previous pump is still "
                        "stopping; deferring restart until it exits",
                        camera_id,
                    )
                    continue

                self._stats[camera_id] = CameraStats()
                self._start_pump(assignment)
                log.info("camera=%s added to assignment", camera_id)

            overflow = added_ids[available:]
            if overflow:
                log.warning(
                    "assignment refresh: %d new camera(s) exceed max_active_cameras=%d; "
                    "not pulling %s",
                    len(overflow),
                    self._s.max_active_cameras,
                    overflow,
                )

            self._reap_and_restart_pumps()

    def _reap_and_restart_pumps(self) -> None:
        """Handle every pump thread that has actually finished.

        A finished thread means one of two different things, and they get
        different treatment:

        * The camera is no longer assigned (removed by reconciliation, which
          only ever calls `request_stop()` and lets the pump unwind on its
          own time) -- drop tracking. This is what keeps a long-running
          worker from leaking a thread and a stats entry per camera churn.

        * The camera is STILL assigned. The pump ended on its own: an
          exception escaped its own try/except, or `capture.frames()`
          returned normally because the catalogue reports the camera as not
          live (I1). Neither case is reachable through `added_ids` in
          `_reconcile_assignments` -- that only starts pumps for camera ids
          not yet in `_assignments`, and this one still is -- so left alone
          it never restarts and permanently occupies a `max_active_cameras`
          slot. Restart it, throttled by a per-camera backoff so a camera
          that fails the same way on every attempt is not restarted on every
          reconciliation tick forever.

        Caller must hold `self._lock`.
        """
        finished = [
            camera_id for camera_id, thread in self._pump_threads.items() if not thread.is_alive()
        ]
        now = time.monotonic()
        for camera_id in finished:
            if camera_id not in self._assignments:
                del self._pump_threads[camera_id]
                self._captures.pop(camera_id, None)
                self._stats.pop(camera_id, None)
                self._pump_backoff.pop(camera_id, None)
                self._pump_retry_at.pop(camera_id, None)
                continue

            retry_at = self._pump_retry_at.get(camera_id)
            if retry_at is not None and now < retry_at:
                # Still cooling down. Leave the dead thread tracked exactly
                # as it is -- `_touch_liveness` (I2) reading `is_alive()`
                # on it must keep seeing a dead pump for an assigned camera,
                # not an entry that quietly vanished while we wait.
                continue

            backoff = self._pump_backoff.get(camera_id, self._s.backoff_initial_s)
            self._pump_retry_at[camera_id] = now + backoff
            self._pump_backoff[camera_id] = min(backoff * 2.0, self._s.backoff_max_s)
            log.warning(
                "camera=%s pump died while still assigned; restarting (next restart backoff %.1fs)",
                camera_id,
                backoff,
            )
            del self._pump_threads[camera_id]
            self._captures.pop(camera_id, None)
            # Fresh stats for a fresh attempt -- carrying over the dead
            # pump's last_error/consecutive_failures would report a crash
            # from a previous attempt as the current state indefinitely.
            self._stats[camera_id] = CameraStats()
            self._start_pump(self._assignments[camera_id])

    # --- heartbeats ----------------------------------------------------------

    def _report_once(self) -> None:
        wid = worker_id()
        now = datetime.now(UTC).isoformat()
        # Snapshot the running set under the lock, then do the (slow, network)
        # heartbeats outside it — reconciliation only ever runs on this same
        # thread, but `stop()` can run on another one at any moment.
        with self._lock:
            assignments = list(self._assignments.values())
            captures = dict(self._captures)
        for assignment in assignments:
            stats = self._stats.get(assignment.camera_id)
            if stats is None:
                continue
            snapshot = stats.snapshot()
            capture = captures.get(assignment.camera_id)
            if capture is not None:
                # I3/I4: `connected` and `consecutive_failures` are read live
                # off the capture, not off whatever the pump last pushed into
                # `stats` -- the pump only runs this update while frames are
                # arriving, and a stalled reconnect leaves `stats` holding
                # values that are old for exactly as long as the stall lasts.
                snapshot["connected"] = capture.connected
                snapshot["consecutive_failures"] = capture.consecutive_failures
                # `last_error` prefers a crash reason the pump itself recorded
                # (a real exception, more specific than anything the capture
                # knows) over the capture's own read-failure message; only
                # fall back to the capture's when the pump has not set one.
                if snapshot["last_error"] is None:
                    snapshot["last_error"] = capture.last_error
            payload = {
                "worker_id": wid,
                "observed_at": now,
                **snapshot,
            }
            try:
                self._registry.heartbeat(assignment.camera_id, payload)
            except httpx.HTTPError as exc:
                # A registry blip must not stop ingest. The camera goes stale in
                # the registry's own view — which is correct, because from the
                # registry's side nothing is arriving — and recovers on the next
                # successful report.
                log.warning("heartbeat for camera=%s failed: %s", assignment.camera_id, exc)

    def _touch_liveness(self) -> None:
        """Prove the reporter loop is still turning AND that it is turning
        pumps, not just heartbeats.

        A worker exposes no HTTP port, so a liveness probe has nothing to GET.
        The failure that matters, and is otherwise invisible, is the process
        alive, the pod Ready, and every pump thread dead (I2) -- the reporter
        thread and the pump threads are independent, so the reporter turning
        on schedule proves nothing about whether any camera is actually being
        pulled. Touch the file only when every currently assigned camera has
        a live pump thread; a dead one is left to `_reap_and_restart_pumps`,
        but the probe must not read "Ready" for as long as that repair is
        pending.
        """
        with self._lock:
            all_pumps_alive = all(
                camera_id in self._pump_threads and self._pump_threads[camera_id].is_alive()
                for camera_id in self._assignments
            )
        if not all_pumps_alive:
            log.warning(
                "not touching liveness file: at least one assigned camera has no live pump thread"
            )
            return
        try:
            with open(self._s.liveness_file, "w") as fh:
                fh.write(str(time.time()))
        except OSError as exc:
            # A read-only /tmp is a deployment problem, not an ingest problem.
            log.warning("could not write liveness file %s: %s", self._s.liveness_file, exc)

    def _report_loop(self) -> None:
        self._touch_liveness()
        while not self._stop.wait(self._s.heartbeat_interval_s):
            if self._s.assignment_refresh:
                self._reconcile_assignments()
            self._report_once()
            self._touch_liveness()

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if not self._assignments:
            log.warning("no cameras assigned; worker idle")
        # Nothing else touches these dicts until the reporter thread starts
        # below, so no lock is needed for this first batch.
        for assignment in list(self._assignments.values()):
            self._start_pump(assignment)

        self._reporter_thread = threading.Thread(target=self._report_loop, name="heartbeat")
        self._reporter_thread.daemon = True
        self._reporter_thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            captures = list(self._captures.values())
            pump_threads = list(self._pump_threads.values())
            assignments = list(self._assignments.values())
        # Ask, do not release. Each pump thread owns its capture and releases it
        # in its own `finally`; tearing the handle out from under a blocked
        # `read()` is an OpenCV crash rather than an exception, and every pod
        # takes a SIGTERM sooner or later.
        for capture in captures:
            capture.request_stop()
        for thread in pump_threads:
            thread.join(timeout=5.0)
        if self._reporter_thread is not None:
            self._reporter_thread.join(timeout=5.0)
        # Every pump thread has stopped submitting by now, so this is the
        # last batch that will ever be pending: stop the flush thread and
        # drain it, rather than dropping up to a batch's worth of already-
        # detected vehicles on the floor at shutdown.
        self._batcher.close()
        self._match_client.close()
        # One last report, so a clean shutdown shows as "worker gone" promptly
        # rather than waiting out the staleness window.
        for assignment in assignments:
            stats = self._stats.get(assignment.camera_id)
            if stats is not None:
                stats.update(connected=False, last_error="worker shutting down")
        self._report_once()
        self._registry.close()

    def wait(self) -> None:
        while not self._stop.is_set():
            time.sleep(0.5)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = ingest_settings()
    registry = RegistryClient(settings)

    assignments = registry.assignments()
    worker = IngestWorker(assignments, settings=settings, registry=registry)

    def shutdown(signum, _frame):
        log.info("signal %s: shutting down", signum)
        worker.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    worker.start()
    worker.wait()


if __name__ == "__main__":
    main()
