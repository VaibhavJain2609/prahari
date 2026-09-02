"""`StreamCapture`'s live `connected` / `consecutive_failures` / `last_error`
state (I3/I4).

`cv2.VideoCapture` is swapped for a scripted fake, so these run with no
network and no real decoder. The fake's `read()` pops from a fixed script of
(ok, image) pairs; once exhausted it fails forever, so a test can drive a
capture from "reading fine" to "gave up and is backing off" deterministically.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest
from prahari_common.catalogue import CameraEntry

from prahari_inference import capture as capture_module
from prahari_inference.config import IngestSettings

_FRAME = np.zeros((2, 2, 3), dtype=np.uint8)


class FakeVideoCapture:
    """Replaces `cv2.VideoCapture`. `script` is a list of booleans: True pops
    a frame, False fails the read. Once the script is exhausted, every
    further read fails -- modelling a feed that never comes back."""

    def __init__(self, script: list[bool]) -> None:
        self._script = list(script)
        self._pos_ms = 0.0

    def isOpened(self) -> bool:  # noqa: N802 - matches cv2's API
        return True

    def read(self):
        self._pos_ms += 40.0
        ok = self._script.pop(0) if self._script else False
        return ok, (_FRAME if ok else None)

    def get(self, _prop) -> float:
        return self._pos_ms

    def release(self) -> None:
        pass


def _camera(*, live: bool = True) -> CameraEntry:
    return CameraEntry(id="cam-1", name="Test Camera", live=live)


def _settings(**overrides) -> IngestSettings:
    base = dict(join_grace_s=0.0, read_failure_threshold=2, backoff_initial_s=0.01)
    base.update(overrides)
    return IngestSettings(**base)


def _patch_cv2(monkeypatch: pytest.MonkeyPatch, script: list[bool]) -> None:
    monkeypatch.setattr(
        capture_module.cv2, "VideoCapture", lambda *a, **k: FakeVideoCapture(script)
    )


class TestConnectedTracksReality:
    def test_connected_is_true_only_after_a_successful_read(self, monkeypatch):
        _patch_cv2(monkeypatch, [True, True, True])
        cap = capture_module.StreamCapture(_camera(), ingest=_settings(), url="rtsp://x")
        assert cap.connected is False  # never opened yet

        frames = cap.frames()
        next(frames)
        assert cap.connected is True

    def test_connected_clears_the_instant_the_read_loop_gives_up(self, monkeypatch):
        # One good frame, then the feed dies forever. `read_failure_threshold=2`
        # means the second consecutive failure ends the inner loop and the
        # capture starts trying to reconnect -- which never stops on its own,
        # so we drive the generator on a background thread and observe
        # `connected` flip to False from the main thread, then unwind it.
        _patch_cv2(monkeypatch, [True])
        cap = capture_module.StreamCapture(_camera(), ingest=_settings(), url="rtsp://x")
        frames = cap.frames()
        next(frames)
        assert cap.connected is True

        pump = threading.Thread(target=lambda: list(frames), daemon=True)
        pump.start()
        try:
            deadline = time.monotonic() + 2.0
            while cap.connected and time.monotonic() < deadline:
                time.sleep(0.01)
            assert cap.connected is False
        finally:
            cap.request_stop()
            pump.join(timeout=2.0)

    def test_close_forces_connected_false_regardless_of_generator_state(self, monkeypatch):
        """`close()` is the one call every exit path funnels through -- it
        must not depend on the generator having noticed anything first."""
        _patch_cv2(monkeypatch, [True, True, True])
        cap = capture_module.StreamCapture(_camera(), ingest=_settings(), url="rtsp://x")
        next(cap.frames())
        assert cap.connected is True

        cap.close()
        assert cap.connected is False


class TestFailureCounterAndLastError:
    def test_consecutive_failures_resets_on_the_next_good_read(self, monkeypatch):
        _patch_cv2(monkeypatch, [True, False, True])
        cap = capture_module.StreamCapture(_camera(), ingest=_settings(), url="rtsp://x")
        frames = cap.frames()
        next(frames)
        assert cap.consecutive_failures == 0
        # The False read happens inside the grace-window/threshold check and
        # is retried in-process (join_grace_s=0, threshold=2), so the second
        # `next()` observes the SECOND True without the caller seeing the dip.
        next(frames)
        assert cap.consecutive_failures == 0
        assert cap.last_error is None

    def test_last_error_is_set_once_the_read_failure_threshold_is_exceeded(self, monkeypatch):
        # `request_stop()` short-circuits both while-loop conditions, so it
        # must not be called until the failure threshold has actually had a
        # chance to trip -- otherwise the loop exits for the "asked to stop"
        # reason and never touches the failure-counting code at all.
        _patch_cv2(monkeypatch, [True])  # one frame, then failures forever
        cap = capture_module.StreamCapture(_camera(), ingest=_settings(), url="rtsp://x")
        frames = cap.frames()
        next(frames)

        pump = threading.Thread(target=lambda: list(frames), daemon=True)
        pump.start()
        try:
            deadline = time.monotonic() + 2.0
            while cap.last_error is None and time.monotonic() < deadline:
                time.sleep(0.01)

            assert cap.consecutive_failures >= 2
            assert cap.last_error is not None
            assert "consecutive" in cap.last_error
        finally:
            cap.request_stop()
            pump.join(timeout=2.0)


class TestNotLiveCatalogue:
    def test_a_camera_the_catalogue_calls_dead_sets_a_distinguishing_last_error(self):
        """Without this, a not-live camera reports connected=false,
        last_error=null, frames_decoded=0 forever -- indistinguishable from
        one still starting up (I1's quieter variant)."""
        cap = capture_module.StreamCapture(_camera(live=False), ingest=_settings(), url="rtsp://x")
        frames = list(cap.frames())

        assert frames == []
        assert cap.connected is False
        assert cap.last_error is not None
