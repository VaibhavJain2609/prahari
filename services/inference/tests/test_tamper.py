"""The tamper detector, exercised with synthetic frames only.

No camera, no model weights: every frame here is a hand-built `np.ndarray`, and
every timing value is a hand-built `FrameTiming`. What is under test is the one
invariant the design calls out by name — a loop cut must not fire the
scene-change signal — plus the two threshold detectors that ride alongside it.
"""

from __future__ import annotations

import numpy as np

from prahari_inference.config import DetectorSettings
from prahari_inference.detect.tamper import TamperDetector
from prahari_inference.detect.types import SampledFrame
from prahari_inference.timing import FrameTiming

SETTINGS = DetectorSettings(
    black_frame_luma=16.0,
    black_frame_ratio_alert=0.9,
    tamper_blur_variance=40.0,
    tamper_settle_frames=15,
)


def timing(*, pts_ms: float, loop_epoch: int = 0, discontinuity: bool = False) -> FrameTiming:
    return FrameTiming(
        pts_ms=pts_ms,
        delta_ms=33.0,
        wall_clock=pts_ms / 1000.0,
        loop_epoch=loop_epoch,
        replaying=False,
        discontinuity=discontinuity,
    )


def bright_frame(seed: int = 0, size: int = 64) -> np.ndarray:
    """A textured, well-lit frame. Noise so the Laplacian variance clears the
    blur threshold and a fixed seed so a test can compare two "different"
    frames deterministically."""
    rng = np.random.default_rng(seed)
    return rng.integers(80, 220, size=(size, size), dtype=np.uint8)


def dark_frame(size: int = 64) -> np.ndarray:
    return np.full((size, size), 4, dtype=np.uint8)


def blurred_frame(size: int = 64) -> np.ndarray:
    """Flat field: zero Laplacian variance, well above the black-frame luma
    floor, so it isolates the blur signal from the black-frame one."""
    return np.full((size, size), 128, dtype=np.uint8)


def sample(image: np.ndarray, frame_timing: FrameTiming, camera_id: str = "cam-1") -> SampledFrame:
    return SampledFrame(camera_id=camera_id, image=image, timing=frame_timing)


# --- black frame --------------------------------------------------------------


def test_black_frame_ratio_rises_as_dark_frames_accumulate():
    detector = TamperDetector(SETTINGS)
    report = None
    for i in range(30):
        report = detector.observe(sample(dark_frame(), timing(pts_ms=i * 100)))
    assert report.black_frame_ratio == 1.0
    assert report.suspected
    assert "black frame" in report.reason


def test_a_single_dark_frame_does_not_alert_the_black_frame_ratio_on_its_own():
    """The ratio is over a rolling window; one dark frame in an otherwise lit
    stream must not push the ratio itself past the alert threshold. (The same
    frame can still be `suspected` via blur or scene-change — those are
    separate signals, covered by their own tests — this test is only about
    the ratio not spiking from a single sample.)"""
    detector = TamperDetector(SETTINGS)
    for i in range(10):
        detector.observe(sample(bright_frame(i), timing(pts_ms=i * 100)))
    report = detector.observe(sample(dark_frame(), timing(pts_ms=1000)))
    assert report.black_frame_ratio < SETTINGS.black_frame_ratio_alert


def test_black_frame_signal_is_not_suppressed_across_a_loop_boundary():
    """§6, explicit: a feed that loops into darkness is genuinely dark. Only
    the scene-change signal gets the loop whitelist."""
    detector = TamperDetector(SETTINGS)
    for i in range(5):
        detector.observe(sample(bright_frame(i), timing(pts_ms=i * 100, loop_epoch=0)))
    report = None
    for i in range(30):
        frame_timing = timing(pts_ms=1000 + i * 100, loop_epoch=1, discontinuity=(i == 0))
        report = detector.observe(sample(dark_frame(), frame_timing))
    assert report.black_frame_ratio == 1.0
    assert report.suspected


# --- blur / defocus -------------------------------------------------------------


def test_low_laplacian_variance_flags_a_defocused_or_covered_lens():
    detector = TamperDetector(SETTINGS)
    report = detector.observe(sample(blurred_frame(), timing(pts_ms=0)))
    assert report.blur_variance < SETTINGS.tamper_blur_variance
    assert report.suspected
    assert "blur variance" in report.reason


def test_a_textured_frame_clears_the_blur_threshold():
    detector = TamperDetector(SETTINGS)
    report = detector.observe(sample(bright_frame(0), timing(pts_ms=0)))
    assert report.blur_variance >= SETTINGS.tamper_blur_variance


# --- scene change / loop suppression -------------------------------------------


def test_scene_change_fires_on_a_genuine_break_outside_the_settle_window():
    detector = TamperDetector(SETTINGS)
    # Establish a settled baseline, well clear of any epoch change.
    for i in range(20):
        detector.observe(sample(bright_frame(0), timing(pts_ms=i * 100, loop_epoch=0)))
    # A different, unrelated scene — same epoch, no loop cut involved.
    report = detector.observe(sample(dark_frame(), timing(pts_ms=2100, loop_epoch=0)))
    assert report.scene_changed
    assert "scene change" in report.reason


def test_scene_change_is_suppressed_on_the_loop_boundary_frame_itself():
    detector = TamperDetector(SETTINGS)
    detector.observe(sample(bright_frame(0), timing(pts_ms=0, loop_epoch=0)))
    # The cut: content changes completely and loop_epoch increments in the same step.
    cut = timing(pts_ms=100, loop_epoch=1, discontinuity=True)
    report = detector.observe(sample(dark_frame(), cut))
    assert not report.scene_changed


def test_scene_change_is_suppressed_for_settle_frames_after_the_boundary():
    detector = TamperDetector(SETTINGS)
    detector.observe(sample(bright_frame(0), timing(pts_ms=0, loop_epoch=0)))
    detector.observe(sample(dark_frame(), timing(pts_ms=100, loop_epoch=1, discontinuity=True)))
    # Still inside the settle window: content keeps swinging, must stay quiet.
    for i in range(1, SETTINGS.tamper_settle_frames):
        image = bright_frame(i) if i % 2 else dark_frame()
        report = detector.observe(sample(image, timing(pts_ms=100 + i * 100, loop_epoch=1)))
        assert not report.scene_changed, f"fired at settle offset {i}"


def test_scene_change_resumes_once_the_settle_window_elapses():
    detector = TamperDetector(SETTINGS)
    detector.observe(sample(bright_frame(0), timing(pts_ms=0, loop_epoch=0)))
    pts = 100.0
    detector.observe(sample(dark_frame(), timing(pts_ms=pts, loop_epoch=1, discontinuity=True)))
    for _ in range(1, SETTINGS.tamper_settle_frames):
        pts += 100
        detector.observe(sample(dark_frame(), timing(pts_ms=pts, loop_epoch=1)))
    # Settled on a dark, static scene now; a genuine break should fire again.
    pts += 100
    report = detector.observe(sample(bright_frame(99), timing(pts_ms=pts, loop_epoch=1)))
    assert report.scene_changed


def test_two_full_loop_cycles_stay_silent_on_scene_change():
    """The exact failure mode the whitelist exists to prevent: these feeds loop
    and cut on every cycle, every camera. Two full cycles of nothing but loop
    cuts and settle-window frames must produce zero scene-change reports."""

    def scene_for_epoch(epoch: int, size: int = 64) -> np.ndarray:
        """A whole cycle plays one unvarying "scene" — identical frames, so
        nothing inside a cycle can trip the detector on its own — and
        alternating epochs pick a visually distinct scene so the loop point
        really is the abrupt histogram break the design doc describes, not a
        break so mild the suppression logic gets no credit for catching it."""
        rng = np.random.default_rng(epoch)
        low, high = (150, 220) if epoch % 2 == 0 else (20, 70)
        return rng.integers(low, high, size=(size, size), dtype=np.uint8)

    detector = TamperDetector(SETTINGS)
    pts = 0.0
    scene_changes: list[int] = []

    def run_cycle(epoch: int, frame_count: int = 25) -> None:
        nonlocal pts
        frame = scene_for_epoch(epoch)
        for i in range(frame_count):
            pts += 100
            report = detector.observe(
                sample(frame, timing(pts_ms=pts, loop_epoch=epoch, discontinuity=(i == 0)))
            )
            if report.scene_changed:
                scene_changes.append(epoch)

    run_cycle(epoch=0)
    run_cycle(epoch=1)  # first loop cut — a real, sharp histogram break
    run_cycle(epoch=2)  # second loop cut — same break, other direction

    assert scene_changes == []


def test_black_frame_reason_and_blur_reason_can_combine():
    settings = DetectorSettings(
        black_frame_luma=16.0,
        black_frame_ratio_alert=0.5,
        tamper_blur_variance=40.0,
        tamper_settle_frames=15,
    )
    detector = TamperDetector(settings)
    report = None
    for i in range(30):
        report = detector.observe(sample(dark_frame(), timing(pts_ms=i * 100)))
    assert report.suspected
    assert "black frame ratio" in report.reason
    assert "blur variance" in report.reason


def test_a_fresh_detector_reports_no_suspicion_on_a_normal_frame():
    detector = TamperDetector(SETTINGS)
    report = detector.observe(sample(bright_frame(0), timing(pts_ms=0)))
    assert not report.suspected
    assert report.reason is None
