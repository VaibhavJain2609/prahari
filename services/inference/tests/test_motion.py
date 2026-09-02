"""Tests for the motion gate.

No camera and no model weights: a `SampledFrame` is built directly from a
synthetic `np.ndarray`, and `FrameTiming` is constructed by hand rather than
routed through `PTSClock` — only `loop_epoch` and `delta_ms` matter here, and
`test_timing.py` already covers how those get produced.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from prahari_inference.config import DetectorSettings
from prahari_inference.detect.motion import MotionGate
from prahari_inference.detect.types import SampledFrame
from prahari_inference.timing import FrameTiming

_HEIGHT, _WIDTH = 90, 160


def _timing(*, loop_epoch: int = 0, replaying: bool = False) -> FrameTiming:
    return FrameTiming(
        pts_ms=0.0,
        delta_ms=40.0,
        wall_clock=1000.0,
        loop_epoch=loop_epoch,
        replaying=replaying,
        discontinuity=False,
    )


def _frame(
    image: np.ndarray, *, loop_epoch: int = 0, replaying: bool = False, camera_id: str = "cam-1"
) -> SampledFrame:
    return SampledFrame(
        camera_id=camera_id, image=image, timing=_timing(loop_epoch=loop_epoch, replaying=replaying)
    )


def _flat(value: int) -> np.ndarray:
    return np.full((_HEIGHT, _WIDTH, 3), value, dtype=np.uint8)


def _settings(**overrides) -> DetectorSettings:
    return DetectorSettings(**overrides)


class TestWarmup:
    def test_first_frame_always_passes(self):
        gate = MotionGate(_settings(motion_warmup_frames=3))
        assert gate.should_process(_frame(_flat(50)))

    def test_warmup_frames_pass_unconditionally_even_if_identical(self):
        gate = MotionGate(_settings(motion_warmup_frames=3))
        # Identical frames would ordinarily read as zero motion, but the
        # background model is not trustworthy yet — warmup must pass them
        # regardless of content.
        for _ in range(4):  # 1 seed + 3 warmup
            assert gate.should_process(_frame(_flat(50)))


class TestSteadyState:
    def test_static_scene_is_dropped(self):
        gate = MotionGate(_settings(motion_warmup_frames=1, motion_threshold=0.01))
        for _ in range(3):  # seed + warmup
            gate.should_process(_frame(_flat(50)))

        assert not gate.should_process(_frame(_flat(50)))

    def test_large_change_passes(self):
        gate = MotionGate(_settings(motion_warmup_frames=1, motion_threshold=0.01))
        for _ in range(3):
            gate.should_process(_frame(_flat(50)))

        half = _flat(50)
        half[:, _WIDTH // 2 :, :] = 220  # half the frame changes sharply
        assert gate.should_process(_frame(half))

    def test_frames_seen_and_passed_are_counted(self):
        gate = MotionGate(_settings(motion_warmup_frames=2, motion_threshold=0.01))
        for _ in range(3):
            gate.should_process(_frame(_flat(50)))
        gate.should_process(_frame(_flat(50)))  # static, dropped

        assert gate.frames_seen == 4
        assert gate.frames_passed == 3
        assert gate.skip_ratio == pytest.approx(0.25)


class TestEpochScoping:
    def test_epoch_change_resets_the_background_and_forces_a_new_warmup(self):
        gate = MotionGate(_settings(motion_warmup_frames=2, motion_threshold=0.01))
        for _ in range(4):
            gate.should_process(_frame(_flat(50), loop_epoch=0))
        assert not gate.should_process(_frame(_flat(50), loop_epoch=0))  # steady state, dropped

        # The loop cut: same pixel content, new epoch. Must pass unconditionally
        # rather than reading as "no motion" against the stale pre-cut model.
        assert gate.should_process(_frame(_flat(200), loop_epoch=1))
        assert gate.should_process(_frame(_flat(200), loop_epoch=1))  # still warming up

    def test_skip_ratio_survives_across_an_epoch_reset(self):
        gate = MotionGate(_settings(motion_warmup_frames=2, motion_threshold=0.01))
        for _ in range(3):
            gate.should_process(_frame(_flat(50), loop_epoch=0))
        gate.should_process(_frame(_flat(50), loop_epoch=0))  # dropped

        gate.should_process(_frame(_flat(50), loop_epoch=1))  # epoch reset, passes

        assert gate.frames_seen == 5
        assert gate.frames_passed == 4


class TestReplayBurst:
    def test_a_replaying_frame_passes_unconditionally_even_with_a_settled_background(self):
        """V4 / DAY2-DESIGN.md §4.2: never gate on `replaying=True` frames for
        motion magnitude. A GOP replay burst delivers frames far faster than
        real time, so the diff against the running background does not mean
        what it means for a live frame -- it must not be used to skip."""
        gate = MotionGate(_settings(motion_warmup_frames=1, motion_threshold=0.01))
        for _ in range(3):  # seed + warmup, background settles on a static scene
            gate.should_process(_frame(_flat(50)))
        assert not gate.should_process(_frame(_flat(50)))  # steady state, would drop

        # Identical pixels -- zero motion -- but replaying=True must still
        # force it through, exactly like the warmup and epoch-reset cases.
        assert gate.should_process(_frame(_flat(50), replaying=True))

    def test_a_replaying_frame_still_counts_toward_frames_passed(self):
        gate = MotionGate(_settings(motion_warmup_frames=1, motion_threshold=0.01))
        for _ in range(3):
            gate.should_process(_frame(_flat(50)))
        gate.should_process(_frame(_flat(50)))  # dropped, not counted as passed

        gate.should_process(_frame(_flat(50), replaying=True))

        assert gate.frames_seen == 5
        assert gate.frames_passed == 3

    def test_live_delivery_resuming_goes_back_to_gating_normally(self):
        """Once `replaying` drops back to False the gate must resume real
        diffing immediately -- the bypass is scoped to the burst, not latched
        for the rest of the epoch."""
        gate = MotionGate(_settings(motion_warmup_frames=1, motion_threshold=0.01))
        for _ in range(3):
            gate.should_process(_frame(_flat(50)))
        gate.should_process(_frame(_flat(50), replaying=True))  # burst frame, forced through

        assert not gate.should_process(_frame(_flat(50)))  # live again: steady state, dropped


class TestConcurrency:
    def test_concurrent_should_process_calls_never_lose_a_frame_count(self):
        # P3: `CrossCameraBatcher` can call `should_process` on the SAME
        # gate from two threads at once (a pump thread and the flush
        # thread both handling a batch containing this camera). Without a
        # lock, `frames_seen`/`frames_passed` increments race and the
        # background read-then-write can interleave across threads.
        # `frames_seen` must equal the exact number of calls made, every
        # time -- a lost increment here is a corrupted `skip_ratio`, the
        # number the streams-per-GPU argument rests on.
        gate = MotionGate(_settings(motion_warmup_frames=2, motion_threshold=0.01))
        calls = 200

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(lambda i: gate.should_process(_frame(_flat(i % 256))), range(calls)))

        assert gate.frames_seen == calls
        assert gate.frames_passed <= calls
