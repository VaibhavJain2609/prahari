"""Tests for the cross-camera batcher.

Every test uses a short `batch_timeout_ms` so the timeout-trigger tests run in
well under a second, and closes the batcher in a `finally` so a failed
assertion never leaves a daemon flush thread polling for the rest of the suite.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from prahari_inference.config import DetectorSettings
from prahari_inference.detect.batching import CrossCameraBatcher
from prahari_inference.detect.types import SampledFrame
from prahari_inference.timing import FrameTiming


def _timing() -> FrameTiming:
    return FrameTiming(
        pts_ms=0.0,
        delta_ms=40.0,
        wall_clock=1000.0,
        loop_epoch=0,
        replaying=False,
        discontinuity=False,
    )


def _frame(camera_id: str) -> SampledFrame:
    return SampledFrame(
        camera_id=camera_id, image=np.zeros((2, 2, 3), dtype=np.uint8), timing=_timing()
    )


@pytest.fixture
def batches() -> list[list[SampledFrame]]:
    return []


def _make_batcher(batches: list, **overrides) -> CrossCameraBatcher:
    settings = DetectorSettings(**overrides)
    return CrossCameraBatcher(on_batch=batches.append, settings=settings)


class TestSizeTrigger:
    def test_flushes_as_soon_as_batch_size_is_reached(self, batches):
        batcher = _make_batcher(batches, batch_size=3, batch_timeout_ms=5_000)
        try:
            batcher.submit(_frame("cam-1"))
            batcher.submit(_frame("cam-2"))
            assert batches == [], "must not flush before batch_size is reached"
            batcher.submit(_frame("cam-3"))
            assert len(batches) == 1
            assert len(batches[0]) == 3
        finally:
            batcher.close()

    def test_fills_across_cameras_not_within_one(self, batches):
        # The whole point of cross-camera batching: frames from different
        # cameras combine into a single batch rather than needing batch_size
        # frames from any one camera.
        batcher = _make_batcher(batches, batch_size=2, batch_timeout_ms=5_000)
        try:
            batcher.submit(_frame("cam-1"))
            batcher.submit(_frame("cam-2"))
            assert len(batches) == 1
            camera_ids = {f.camera_id for f in batches[0]}
            assert camera_ids == {"cam-1", "cam-2"}
        finally:
            batcher.close()


class TestTimeoutTrigger:
    def test_flushes_a_partial_batch_after_the_timeout(self, batches):
        batcher = _make_batcher(batches, batch_size=10, batch_timeout_ms=50)
        try:
            batcher.submit(_frame("cam-1"))
            deadline = time.monotonic() + 2.0
            while not batches and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(batches) == 1
            assert len(batches[0]) == 1
        finally:
            batcher.close()

    def test_quiet_estate_does_not_wait_forever(self, batches):
        """A camera set smaller than batch_size must not hold frames
        indefinitely — that is an unbounded miss of the alert-latency budget."""
        batcher = _make_batcher(batches, batch_size=100, batch_timeout_ms=50)
        try:
            batcher.submit(_frame("cam-1"))
            batcher.submit(_frame("cam-2"))
            deadline = time.monotonic() + 2.0
            while not batches and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(batches) == 1
            assert len(batches[0]) == 2
        finally:
            batcher.close()


class TestManualFlush:
    def test_flush_emits_whatever_is_pending(self, batches):
        batcher = _make_batcher(batches, batch_size=100, batch_timeout_ms=5_000)
        try:
            batcher.submit(_frame("cam-1"))
            batcher.flush()
            assert len(batches) == 1
            assert len(batches[0]) == 1
        finally:
            batcher.close()

    def test_flush_on_empty_batcher_emits_nothing(self, batches):
        batcher = _make_batcher(batches, batch_size=100, batch_timeout_ms=5_000)
        try:
            batcher.flush()
            assert batches == []
        finally:
            batcher.close()


class TestFlushLoopSurvivesExceptions:
    def test_an_exception_in_on_batch_does_not_kill_the_deadline_flush_thread(self, batches):
        # P1/V3: `on_batch` raising during a timeout-driven flush used to
        # propagate out of `_flush_loop` and end the daemon thread silently.
        # From then on only the size trigger fired, so a quiet estate
        # (active cameras < batch_size) accumulates frames in `_pending`
        # forever with no log, no counter, no liveness signal. The loop must
        # survive one bad batch and keep enforcing the deadline for the next.
        calls = {"n": 0}

        def flaky_on_batch(batch: list[SampledFrame]) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated cascade failure")
            batches.append(batch)

        settings = DetectorSettings(batch_size=100, batch_timeout_ms=50)
        batcher = CrossCameraBatcher(on_batch=flaky_on_batch, settings=settings)
        try:
            batcher.submit(_frame("cam-1"))  # first timeout flush: raises, dropped
            deadline = time.monotonic() + 2.0
            while calls["n"] < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert calls["n"] == 1, "the flaky on_batch was never even invoked once"

            batcher.submit(_frame("cam-2"))  # second timeout flush: must still fire
            deadline = time.monotonic() + 2.0
            while not batches and time.monotonic() < deadline:
                time.sleep(0.01)

            assert len(batches) == 1, "the flush thread died after the first exception"
            assert len(batches[0]) == 1
            assert batches[0][0].camera_id == "cam-2"
        finally:
            batcher.close()


class TestClose:
    def test_close_flushes_the_pending_batch(self, batches):
        # P2: close() used to set _stop and join without ever calling
        # flush(), silently discarding up to batch_size - 1 sampled frames
        # on every shutdown.
        batcher = _make_batcher(batches, batch_size=100, batch_timeout_ms=5_000)
        batcher.submit(_frame("cam-1"))
        batcher.submit(_frame("cam-2"))
        assert batches == [], "neither trigger should have fired yet"

        batcher.close()

        assert len(batches) == 1
        assert len(batches[0]) == 2

    def test_close_on_an_empty_batcher_emits_nothing(self, batches):
        batcher = _make_batcher(batches, batch_size=100, batch_timeout_ms=5_000)
        batcher.close()
        assert batches == []


class TestConcurrency:
    def test_concurrent_submits_never_split_or_duplicate_a_frame(self, batches):
        """Every camera's pump thread calls submit() independently. No frame
        may be dropped, duplicated, or land in two batches under contention."""
        batcher = _make_batcher(batches, batch_size=8, batch_timeout_ms=200)
        total_frames = 400
        try:
            threads = [
                threading.Thread(target=batcher.submit, args=(_frame(f"cam-{i % 8}"),))
                for i in range(total_frames)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            batcher.flush()

            delivered = sum(len(b) for b in batches)
            assert delivered == total_frames
        finally:
            batcher.close()
