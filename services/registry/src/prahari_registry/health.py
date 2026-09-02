"""Deriving camera health from worker heartbeats.

Everything in this module is a pure function of a heartbeat plus a little
history, so the interesting rules are testable without a database, a camera or
a network. The one health rule that is NOT here is staleness — "the heartbeats
stopped arriving" cannot be computed by the code that runs when a heartbeat
arrives, so it is applied in SQL at read time by the `camera_current` view.

Two hazards from the integrator's guide shape the rules below:

* **The feeds loop.** Each wrap is an abrupt scene cut "similar to a camera
  reboot", on every camera, every cycle. Any rule that fires on a single black
  frame or a single scene change will fire on every camera forever and be
  switched off by the operator within a day. Tamper therefore requires the
  worker's suspicion to *persist* across several heartbeats.

* **The declared frame rate lies.** So drift is measured against the camera's
  own recent median delivery rate, not against the catalogue's claim. The
  declared value is still reported, as an observation about the catalogue's
  accuracy rather than as a health input.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from .models import HealthState, Heartbeat


@dataclass(frozen=True)
class HealthPolicy:
    """Thresholds. Defaults mirror RegistrySettings; passed explicitly so tests
    state the policy they are testing instead of inheriting the environment."""

    fps_drift_ratio: float = 0.6
    """Below this fraction of its own baseline, a camera is degraded. 0.6 is
    loose on purpose — CCTV delivery is bursty and a tight threshold produces an
    alert storm that trains operators to ignore the console."""

    fps_baseline_min_samples: int = 5
    black_frame_ratio: float = 0.9
    tamper_confirm_heartbeats: int = 3


@dataclass(frozen=True)
class HealthVerdict:
    state: HealthState
    reason: str
    baseline_fps: float | None = None


def baseline_fps(samples: Sequence[float], policy: HealthPolicy) -> float | None:
    """The camera's own normal delivery rate.

    Median, not mean: a single stall drags a mean down far enough to make a
    healthy camera look degraded, and the alert that produces is
    indistinguishable from a real fault.

    Returns None until there are enough samples — an unmeasured baseline must
    never be treated as a baseline of zero, which would mark every camera
    healthy, or as a baseline of infinity, which would mark every camera down.
    """
    usable = [s for s in samples if s is not None and s > 0]
    if len(usable) < policy.fps_baseline_min_samples:
        return None
    return statistics.median(usable)


def tamper_streak(flags: Sequence[bool]) -> int:
    """Length of the run of tamper suspicions ending at the most recent report.

    `flags` is newest-first. A run, not a count: three suspicions scattered over
    an hour are three loop wraps, whereas three in a row is a lens that has
    stayed covered.
    """
    streak = 0
    for flag in flags:
        if not flag:
            break
        streak += 1
    return streak


def derive_state(
    heartbeat: Heartbeat,
    *,
    recent_fps: Sequence[float] = (),
    recent_tamper_flags: Sequence[bool] = (),
    policy: HealthPolicy | None = None,
) -> HealthVerdict:
    """Decide a camera's state from one heartbeat and its recent history.

    `recent_fps` and `recent_tamper_flags` are the camera's PREVIOUS heartbeats,
    newest first, excluding this one. Excluding it matters: a camera whose rate
    has just collapsed must be compared against how it used to behave, not
    against an average that already contains the collapse.
    """
    p = policy or HealthPolicy()
    base = baseline_fps(recent_fps, p)

    # Order matters. Each rule below is checked only once the ones above it have
    # been ruled out, because a disconnected camera has no meaningful frame rate
    # and a covered lens is not a bandwidth problem.

    if not heartbeat.connected:
        reason = heartbeat.last_error or "worker reports the stream is not connected"
        return HealthVerdict(HealthState.UNREACHABLE, reason, base)

    streak = tamper_streak([heartbeat.tamper_suspected, *recent_tamper_flags])
    if streak >= p.tamper_confirm_heartbeats:
        return HealthVerdict(
            HealthState.TAMPERED,
            f"tamper suspected on {streak} consecutive heartbeats",
            base,
        )

    blackout = heartbeat.black_frame_ratio
    if blackout is not None and blackout >= p.black_frame_ratio:
        return HealthVerdict(
            HealthState.DEGRADED,
            f"{blackout:.0%} of sampled frames are black",
            base,
        )

    if heartbeat.measured_fps is None:
        # Connected and delivering, but PTSClock has not yet seen the ~10 frames
        # it needs to measure a rate. Not a fault, and must not read as one on a
        # console: every camera passes through this state at every reconnect.
        return HealthVerdict(HealthState.HEALTHY, "connected; frame rate not yet measured", base)

    if base is not None and heartbeat.measured_fps < base * p.fps_drift_ratio:
        return HealthVerdict(
            HealthState.DEGRADED,
            f"delivering {heartbeat.measured_fps:.1f} fps against a baseline of {base:.1f}",
            base,
        )

    if heartbeat.tamper_suspected:
        # Suspected but not yet confirmed. Surfaced in the reason so an operator
        # can see it building, without the state itself flapping on every loop.
        return HealthVerdict(
            HealthState.HEALTHY,
            f"tamper suspected ({streak}/{p.tamper_confirm_heartbeats} heartbeats)",
            base,
        )

    return HealthVerdict(HealthState.HEALTHY, f"delivering {heartbeat.measured_fps:.1f} fps", base)
