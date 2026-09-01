"""Tests for the three timing hazards in §3 of the integrator's guide.

These run without a network or a camera. That is deliberate: the failure modes
they cover (impossible velocities after connect, tamper alerts at every loop
point) are silent in production, so they must be caught here rather than by
watching a live feed and hoping to notice.
"""

from __future__ import annotations

import pytest

from prahari_inference.capture import SampleGate
from prahari_inference.timing import PTSClock


class TestGOPReplayBurst:
    """"The first second or two of frames may arrive faster than real time.
    A tracker that timestamps by arrival will compute impossible velocities
    immediately after every connection.\""""

    def test_burst_frames_are_flagged_and_excluded_from_motion(self):
        clock = PTSClock()
        wall = 1000.0

        # 2s of buffered GOP delivered in ~40ms of wall time.
        burst = [
            clock.observe(pts, now=wall + i * 0.002)
            for i, pts in enumerate(range(0, 2000, 40))
        ]

        assert all(f.replaying for f in burst[1:]), "burst frames must be flagged"
        assert not any(f.usable_for_motion for f in burst), (
            "no burst frame may feed a velocity calculation"
        )

    def test_burst_clears_once_delivery_reaches_real_time(self):
        clock = PTSClock()
        wall = 1000.0

        for i, pts in enumerate(range(0, 2000, 40)):
            clock.observe(pts, now=wall + i * 0.002)

        # Live delivery resumes: PTS and wall clock now advance together.
        live = None
        for i in range(1, 60):
            live = clock.observe(2000 + i * 40, now=wall + 0.1 + i * 0.04)

        assert live is not None
        assert not live.replaying, "burst must clear once wall clock catches up"
        assert live.usable_for_motion

    def test_burst_does_not_re_arm_on_a_network_stall(self):
        """A stall makes wall time advance while PTS does not. That is the
        opposite of a burst and must not be mistaken for one."""
        clock = PTSClock()
        wall = 1000.0
        for i in range(60):
            clock.observe(i * 40, now=wall + i * 0.04)

        stalled = clock.observe(60 * 40, now=wall + 5.0)  # 3s of nothing, then a frame
        assert not stalled.replaying
        assert not stalled.discontinuity, "a gap is not a disconnect"


class TestLoopCut:
    """"Each feed is a continuous recording that loops. At the loop point the
    scene cuts abruptly, similar to a camera reboot.\""""

    def test_pts_regression_advances_the_loop_epoch(self):
        clock = PTSClock()
        wall = 1000.0
        for i in range(30):
            clock.observe(600_000 + i * 40, now=wall + i * 0.04)
        before = clock.loop_epoch

        looped = clock.observe(0.0, now=wall + 1.3)

        assert looped.discontinuity
        assert looped.loop_epoch == before + 1
        assert looped.delta_ms is None, "no delta spans a discontinuity"

    def test_ordinary_gap_is_not_a_discontinuity(self):
        clock = PTSClock()
        clock.observe(0.0, now=1000.0)
        gap = clock.observe(500.0, now=1000.5)  # half a second of dropped frames

        assert not gap.discontinuity
        assert gap.delta_ms == pytest.approx(500.0)

    def test_reconnect_invalidates_downstream_state(self):
        clock = PTSClock()
        clock.observe(10_000.0, now=1000.0)
        before = clock.loop_epoch

        clock.mark_reconnect()
        first = clock.observe(0.0, now=1030.0)

        assert clock.loop_epoch == before + 1
        assert first.discontinuity
        assert first.replaying, "a reconnect replays a fresh GOP"


class TestMeasuredRate:
    """"CAP_PROP_FPS often does not match the actual delivery rate ... Measure
    the real rate yourself.\""""

    def test_measures_rate_from_pts_not_declared_fps(self):
        clock = PTSClock()
        for i in range(60):
            clock.observe(i * 40.0, now=1000.0 + i * 0.04)  # 25 fps
        assert clock.measured_fps == pytest.approx(25.0, rel=0.01)

    def test_single_long_gap_does_not_drag_the_measurement_down(self):
        """Median, not mean — otherwise one stall makes a healthy camera look
        degraded and the health monitor raises a false alarm."""
        clock = PTSClock()
        pts = 0.0
        for i in range(60):
            pts += 3000.0 if i == 30 else 40.0
            clock.observe(pts, now=1000.0 + i * 0.04)
        assert clock.measured_fps == pytest.approx(25.0, rel=0.05)

    def test_returns_none_before_enough_samples(self):
        clock = PTSClock()
        clock.observe(0.0, now=1000.0)
        assert clock.measured_fps is None


class TestSampleGate:
    def test_gates_on_pts_so_the_burst_does_not_flood_the_detector(self):
        clock = PTSClock()
        gate = SampleGate(sample_fps=2.0)
        wall = 1000.0

        passed = sum(
            gate.should_process(clock.observe(pts, now=wall + i * 0.002))
            for i, pts in enumerate(range(0, 2000, 40))
        )

        # 2s of stream time at 2 fps is ~4 frames. A wall-clock gate would have
        # passed the whole burst through as if it were live footage.
        assert passed <= 5, f"burst leaked {passed} frames into the detector"

    def test_emits_immediately_after_a_loop_cut(self):
        clock = PTSClock()
        gate = SampleGate(sample_fps=2.0)
        gate.should_process(clock.observe(600_000.0, now=1000.0))

        assert gate.should_process(clock.observe(0.0, now=1000.04)), (
            "the new scene must reach the detector without waiting a sample interval"
        )
