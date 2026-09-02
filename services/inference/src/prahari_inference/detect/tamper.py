"""Tamper and black-frame detection.

Per §6 of the Day 2 design: mean luma feeds a rolling black-frame ratio,
Laplacian variance flags a defocused or covered lens, and a histogram
correlation break against the recent frame flags a scene change.

THE LOOP CUT MUST NOT FIRE THE SCENE-CHANGE SIGNAL. These feeds are recordings
that loop, and the wrap is an abrupt cut on every camera, every cycle.
`FrameTiming.loop_epoch` is how we know it happened — the scene-change check is
suppressed across the boundary and for `tamper_settle_frames` afterwards. The
black-frame signal is deliberately NOT suppressed: a feed that loops into
darkness is genuinely dark, and hiding that behind the one condition it
happens to coincide with at a night-time loop point defeats the detector.

Reported, never adjudicated — same rule as camera health (see worker.py). Two
workers on the same camera must not be able to disagree about whether it is
tampered.
"""

from __future__ import annotations

from collections import deque

import numpy as np

from prahari_inference.config import DetectorSettings, detector_settings
from prahari_inference.rtsp_env import cv2

from .types import SampledFrame, TamperReport

# How many recent frames feed `black_frame_ratio`. Independent of
# `tamper_settle_frames`, which paces the scene-change whitelist: a black-frame
# condition is a state that should reflect the last several seconds, not just
# the handful of frames right after a cut.
_BLACK_FRAME_WINDOW = 30

# Histogram correlation below this counts as a scene break. 1.0 is identical
# frames; correlation degrades gracefully as a vehicle moves through frame, so
# the bar sits well below 1.0 rather than immediately under it.
_SCENE_CORRELATION_BREAK = 0.5

_HIST_BINS = 32


def _to_gray(image: np.ndarray) -> np.ndarray:
    return image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def _histogram(gray: np.ndarray) -> np.ndarray:
    hist = cv2.calcHist([gray], [0], None, [_HIST_BINS], [0, 256])
    cv2.normalize(hist, hist)
    return hist


class TamperDetector:
    """Stateful, one instance per camera.

    The rolling black-frame window and the last-seen histogram belong to a
    single stream — sharing an instance across cameras would let one camera's
    scene changes suppress another's, which is worse than not detecting them.
    """

    def __init__(self, settings: DetectorSettings | None = None) -> None:
        self._s = settings or detector_settings()
        self._black_window: deque[bool] = deque(maxlen=_BLACK_FRAME_WINDOW)
        self._last_hist: np.ndarray | None = None
        self._last_epoch: int | None = None
        # Starts "already settled": the settle window exists to whitelist a
        # real loop cut, not the first frame pair of a fresh stream, which is
        # ordinary continuous footage and must not be suppressed.
        self._frames_since_epoch_change = self._s.tamper_settle_frames

    def observe(self, frame: SampledFrame) -> TamperReport:
        gray = _to_gray(frame.image)

        epoch = frame.timing.loop_epoch
        if self._last_epoch is None:
            self._last_epoch = epoch
        elif epoch != self._last_epoch:
            self._last_epoch = epoch
            self._frames_since_epoch_change = 0
        else:
            self._frames_since_epoch_change += 1

        # Black frame: never suppressed, including across a loop boundary.
        is_black = float(gray.mean()) < self._s.black_frame_luma
        self._black_window.append(is_black)
        black_frame_ratio = sum(self._black_window) / len(self._black_window)

        blur_variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())

        hist = _histogram(gray)
        scene_changed = False
        if self._last_hist is not None:
            correlation = cv2.compareHist(self._last_hist, hist, cv2.HISTCMP_CORREL)
            # `frames_since_epoch_change` is 0 on the boundary frame itself, so
            # this covers the cut frame and not only the settling window that
            # follows it.
            within_settle = self._frames_since_epoch_change < self._s.tamper_settle_frames
            if correlation < _SCENE_CORRELATION_BREAK and not within_settle:
                scene_changed = True
        self._last_hist = hist

        reasons: list[str] = []
        if black_frame_ratio >= self._s.black_frame_ratio_alert:
            reasons.append(f"black frame ratio {black_frame_ratio:.2f}")
        if blur_variance < self._s.tamper_blur_variance:
            reasons.append(f"blur variance {blur_variance:.1f}")
        if scene_changed:
            reasons.append("scene change")

        return TamperReport(
            black_frame_ratio=black_frame_ratio,
            blur_variance=blur_variance,
            scene_changed=scene_changed,
            suspected=bool(reasons),
            reason="; ".join(reasons) or None,
        )
