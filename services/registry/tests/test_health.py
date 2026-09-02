"""Health derivation.

These are the rules that decide whether the console shows a red pin, so they are
tested against the hazards that actually produce false pins on these feeds: the
loop cut, the reconnect, and the frame rate that has not been measured yet.
"""

from __future__ import annotations

import pytest

from prahari_registry.health import HealthPolicy, baseline_fps, derive_state, tamper_streak
from prahari_registry.models import HealthState, Heartbeat

POLICY = HealthPolicy()


def hb(**kwargs) -> Heartbeat:
    base = {"worker_id": "worker-1", "connected": True, "measured_fps": 10.0}
    return Heartbeat(**(base | kwargs))


# --- baseline ----------------------------------------------------------------


def test_baseline_needs_enough_samples():
    """An unmeasured baseline must be None, never zero.

    Zero would make `measured_fps < 0 * ratio` false for every camera and mark
    the whole estate healthy — the failure that looks like success.
    """
    assert baseline_fps([10.0, 10.0], POLICY) is None
    assert baseline_fps([10.0] * 5, POLICY) == 10.0


def test_baseline_uses_median_not_mean():
    """One stall must not drag the baseline down.

    Mean of these is 8.4; a subsequent 9 fps report would then look fine against
    a baseline that the stall itself created.
    """
    assert baseline_fps([10.0, 10.0, 10.0, 10.0, 2.0], POLICY) == 10.0


def test_baseline_ignores_zero_and_negative():
    assert baseline_fps([10.0, 10.0, 0.0, 10.0, 10.0, 10.0], POLICY) == 10.0


# --- tamper streak -----------------------------------------------------------


def test_tamper_streak_counts_only_the_current_run():
    """Scattered suspicions are loop wraps; a run is a covered lens."""
    assert tamper_streak([True, False, True, True]) == 1
    assert tamper_streak([True, True, True, False]) == 3
    assert tamper_streak([False, True, True]) == 0


# --- state -------------------------------------------------------------------


def test_disconnected_is_unreachable():
    verdict = derive_state(hb(connected=False, last_error="connection refused"))
    assert verdict.state is HealthState.UNREACHABLE
    assert "connection refused" in verdict.reason


def test_unmeasured_fps_is_healthy_not_degraded():
    """PTSClock needs ~10 frames before it can report a rate, and every camera
    passes through that state after every reconnect. Calling it degraded would
    turn each reconnect into an alert."""
    verdict = derive_state(hb(measured_fps=None), recent_fps=[10.0] * 10)
    assert verdict.state is HealthState.HEALTHY
    assert "not yet measured" in verdict.reason


def test_single_tamper_suspicion_does_not_flip_state():
    """The feeds loop, cutting scene abruptly on every camera every cycle. A
    detector firing on one cut would fire on the whole estate, forever."""
    verdict = derive_state(hb(tamper_suspected=True), recent_tamper_flags=[False, False])
    assert verdict.state is HealthState.HEALTHY
    assert "1/3" in verdict.reason


def test_sustained_tamper_is_tampered():
    verdict = derive_state(hb(tamper_suspected=True), recent_tamper_flags=[True, True, False])
    assert verdict.state is HealthState.TAMPERED
    assert "3 consecutive" in verdict.reason


def test_fps_drift_against_own_baseline_degrades():
    verdict = derive_state(hb(measured_fps=4.0), recent_fps=[10.0] * 8)
    assert verdict.state is HealthState.DEGRADED
    assert "baseline" in verdict.reason


def test_fps_drift_needs_a_baseline_to_judge_against():
    """A camera whose history we do not have yet is not degraded. Two samples
    are not a baseline, and a fresh camera must not arrive on the map red."""
    verdict = derive_state(hb(measured_fps=1.0), recent_fps=[10.0, 10.0])
    assert verdict.state is HealthState.HEALTHY


def test_declared_fps_is_never_the_drift_reference():
    """A camera the catalogue claims runs at 25 fps but has always delivered 8
    is healthy. The declared rate is documented as unreliable; judging against
    it would paint most of the estate degraded on day one."""
    verdict = derive_state(hb(measured_fps=8.0), recent_fps=[8.0] * 10)
    assert verdict.state is HealthState.HEALTHY


def test_blackout_degrades():
    verdict = derive_state(hb(black_frame_ratio=0.97), recent_fps=[10.0] * 10)
    assert verdict.state is HealthState.DEGRADED
    assert "black" in verdict.reason


def test_disconnect_outranks_everything_else():
    """A disconnected camera has no meaningful frame rate, so the fps rules must
    not get a chance to describe it as merely degraded."""
    verdict = derive_state(
        hb(connected=False, measured_fps=0.1, black_frame_ratio=1.0, tamper_suspected=True),
        recent_fps=[10.0] * 10,
        recent_tamper_flags=[True, True, True],
    )
    assert verdict.state is HealthState.UNREACHABLE


@pytest.mark.parametrize("ratio", [0.61, 0.8, 1.0, 1.4])
def test_rates_within_tolerance_stay_healthy(ratio: float):
    verdict = derive_state(hb(measured_fps=10.0 * ratio), recent_fps=[10.0] * 10)
    assert verdict.state is HealthState.HEALTHY
