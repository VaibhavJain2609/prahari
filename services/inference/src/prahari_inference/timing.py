"""Frame timing. The single source of truth for "when did this happen".

Three hazards from the integrator's guide are handled here, and nowhere else:

1. **The GOP replay burst.** "When a client connects, the gateway replays its
   buffered group-of-pictures so the decoder can start at a keyframe. The first
   second or two of frames may therefore arrive faster than real time." Anything
   that timestamps by arrival "will compute impossible velocities immediately
   after every connection" — and every reconnect re-injects it.

2. **Non-uniform cadence.** Frame intervals are not guaranteed uniform, and
   CAP_PROP_FPS "often does not match the actual delivery rate". A gap is not a
   disconnect.

3. **The loop cut.** Each feed is a recording that loops, cutting scene
   abruptly "similar to a camera reboot". Long-lived state must recover from it.

Everything downstream — trackers, Kalman filters, speed and dwell estimates,
evidence timestamps — consumes FrameTiming and never touches the clock itself.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

# A PTS that moves backwards at all is a discontinuity: monotonic timestamps are
# guaranteed within a continuous segment.
_PTS_REGRESSION_TOLERANCE_MS = 0.0

# A forward jump this large is a content discontinuity rather than a dropped
# frame. Chosen well above any plausible network stall (§3 says gaps happen and
# must be tolerated) but below a loop period.
_PTS_FORWARD_JUMP_MS = 10_000.0

# Burst detection compares the RATE at which stream time advances against wall
# clock, over a short sliding window.
#
# Rate, not accumulated lead: during replay the lead is small at first (five
# frames into a 20x burst, PTS is only ~190 ms ahead) and then plateaus once
# live delivery resumes — it never shrinks, because both clocks advance
# together afterwards. So "lead is small" is true at the start of a burst and
# false long after one ends: exactly backwards. The ratio, by contrast, is ~20
# during replay and ~1.0 the instant real-time delivery begins.
_BURST_RATE_RATIO = 1.5
_BURST_WINDOW_FRAMES = 8

# The cost of being wrong is asymmetric: holding the flag a few frames too long
# discards a handful of frames from velocity estimation, whereas dropping it
# early admits impossible velocities into the tracker after every connection.
# So until the window has enough samples to measure a rate, assume replay.
_BURST_MIN_SAMPLES = 4


@dataclass(frozen=True)
class FrameTiming:
    """Timing for one decoded frame. `pts_ms` is authoritative; `wall_clock` is
    for audit and correlation only and must never drive a motion model."""

    pts_ms: float
    """Presentation timestamp from the stream. The only valid time axis."""

    delta_ms: float | None
    """PTS elapsed since the previous frame, or None for the first frame of a
    segment. Feed THIS to Kalman filters and trackers — never a fixed 1/fps."""

    wall_clock: float
    """time.time() at read. Audit trail only."""

    loop_epoch: int
    """Increments on every detected discontinuity. Track ids, background models
    and re-id galleries are scoped to an epoch and reset when it changes."""

    replaying: bool
    """True while this frame is part of the connect-time GOP replay burst.
    Frames with replaying=True arrive faster than real time. They are valid for
    detection but NOT for any velocity, speed or dwell-time derivation."""

    discontinuity: bool
    """True on the first frame after a loop cut or reconnect."""

    @property
    def usable_for_motion(self) -> bool:
        """Whether this frame may contribute to a time-derived metric.

        The burst and the frame straddling a discontinuity both produce
        meaningless deltas. Gate speed/dwell estimation on this, not on frame
        index or elapsed wall time.
        """
        return not self.replaying and not self.discontinuity and self.delta_ms is not None


@dataclass
class PTSClock:
    """Converts raw CAP_PROP_POS_MSEC readings into FrameTiming.

    Stateful and single-stream: one clock per camera capture. Survives
    reconnects — call `mark_reconnect()` so the epoch advances and the burst
    detector re-arms, rather than constructing a new clock and losing the epoch
    counter that downstream state is keyed on.
    """

    loop_epoch: int = 0
    _last_pts_ms: float | None = None
    _segment_first_pts_ms: float | None = None
    _segment_start_wall: float | None = None
    _burst_active: bool = True
    _burst_window: deque[tuple[float, float]] = field(
        default_factory=lambda: deque(maxlen=_BURST_WINDOW_FRAMES)
    )
    _recent_deltas: deque[float] = field(default_factory=lambda: deque(maxlen=120))

    def observe(self, pts_ms: float, *, now: float | None = None) -> FrameTiming:
        """Record a frame. `pts_ms` comes from CAP_PROP_POS_MSEC (OpenCV), the
        buffer PTS (GStreamer), or the RTP timestamp."""
        now = time.time() if now is None else now

        discontinuity = False
        delta_ms: float | None = None

        if self._last_pts_ms is None:
            discontinuity = True  # first frame of a segment
        else:
            raw_delta = pts_ms - self._last_pts_ms
            if raw_delta < -_PTS_REGRESSION_TOLERANCE_MS or raw_delta > _PTS_FORWARD_JUMP_MS:
                # Loop point, feed restart, or a gateway-side seek. Either way
                # the content is discontinuous: nothing learned before this
                # frame describes what comes after it.
                discontinuity = True
                self.loop_epoch += 1
                self._recent_deltas.clear()
            else:
                delta_ms = raw_delta
                if raw_delta > 0:
                    self._recent_deltas.append(raw_delta)

        if discontinuity:
            self._segment_first_pts_ms = pts_ms
            self._segment_start_wall = now
            # A cut is also a fresh decoder start, so the burst can recur.
            self._burst_active = True
            self._burst_window.clear()

        self._last_pts_ms = pts_ms

        return FrameTiming(
            pts_ms=pts_ms,
            delta_ms=delta_ms,
            wall_clock=now,
            loop_epoch=self.loop_epoch,
            replaying=self._update_burst(pts_ms, now),
            discontinuity=discontinuity,
        )

    def _update_burst(self, pts_ms: float, now: float) -> bool:
        """Burst detection by measured delivery rate.

        During GOP replay the decoder emits buffered frames as fast as it can,
        so stream time advances many times faster than wall clock. When live
        delivery resumes the ratio drops to ~1.0. Measuring the ratio adapts to
        however much the gateway actually had buffered, instead of guessing
        "about two seconds" and being wrong on a slow camera.

        Latching matters: once we have seen real-time delivery we never
        re-enter burst mode without a discontinuity. A network stall makes wall
        time advance while PTS does not — a ratio well *below* 1.0 — and must
        not be confused with replay.
        """
        if not self._burst_active:
            return False

        self._burst_window.append((pts_ms, now))
        if len(self._burst_window) < _BURST_MIN_SAMPLES:
            return True

        first_pts, first_wall = self._burst_window[0]
        stream_span_ms = pts_ms - first_pts
        wall_span_ms = (now - first_wall) * 1000.0

        if wall_span_ms <= 0:
            # Frames delivered within one clock tick: unambiguously faster than
            # real time.
            return True

        if stream_span_ms / wall_span_ms <= _BURST_RATE_RATIO:
            self._burst_active = False
            self._burst_window.clear()
            return False
        return True

    def mark_reconnect(self) -> None:
        """Call after re-establishing a capture.

        The new connection replays a fresh GOP and its PTS may restart from
        zero, so downstream state must be treated as invalid — the same
        contract as a loop cut.
        """
        self.loop_epoch += 1
        self._last_pts_ms = None
        self._segment_first_pts_ms = None
        self._segment_start_wall = None
        self._burst_active = True
        self._burst_window.clear()
        self._recent_deltas.clear()

    @property
    def measured_fps(self) -> float | None:
        """Delivery rate measured from PTS deltas over a rolling window.

        The guide is explicit that the *declared* rate lies: "Measure the real
        rate yourself, or ignore declared frame rate entirely." This is that
        measurement. Returns None until enough frames have been seen.

        Uses the median, not the mean — a single long gap would drag a mean
        down and make a healthy camera look degraded to the health monitor.
        """
        if len(self._recent_deltas) < 10:
            return None
        ordered = sorted(self._recent_deltas)
        mid = len(ordered) // 2
        median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0
        return 1000.0 / median if median > 0 else None
