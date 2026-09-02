"""Motion gating — the cheapest stage in the cascade, and the one that decides
how many streams a GPU can carry.

Downscaled greyscale frame differencing against a running background, with a
dilation pass so a moving vehicle's low-contrast parts (glass, dark panels)
don't fragment the changed region into specks that individually fall under
`motion_threshold`.

Epoch-scoped, not merely time-scoped: `FrameTiming.loop_epoch` changing means
the scene cut, and the background model built against the previous scene is
worse than useless against the new one — it reads as near-total motion on the
first post-cut frame and stays biased for as long as the stale model persists.
Reset on every epoch change and pass frames unconditionally through the
warmup window, because a background estimate needs a few frames before it
means anything at all.
"""

from __future__ import annotations

import threading

import numpy as np

from prahari_inference.config import DetectorSettings, detector_settings
from prahari_inference.rtsp_env import cv2

from .types import SampledFrame

# Downscale before differencing: vehicle-scale motion survives this resolution
# and sensor noise mostly does not, so the changed-fraction estimate is both
# cheaper and less twitchy than doing it at native resolution.
_GATE_WIDTH = 160
_GATE_HEIGHT = 90

# A per-pixel intensity delta below this is treated as noise, not change.
# Chosen well below a moving vehicle's contrast against typical CCTV
# background (road, footpath) and well above compression-artifact jitter on a
# static scene.
_PIXEL_DELTA_THRESHOLD = 25

_DILATE_KERNEL = np.ones((5, 5), np.uint8)

# Exponential update rate for the running background. Slow enough that a
# vehicle sitting still for a few frames does not get absorbed into the
# background and silently stop registering as motion; fast enough that a
# genuine lighting change (cloud, streetlight) does not permanently bias the
# gate toward "everything is motion".
_BACKGROUND_ALPHA = 0.05


class MotionGate:
    """Per-instance motion state for ONE camera.

    `DetectionPipeline` keys one of these per `camera_id` (see pipeline.py) —
    a cross-camera batch means a single shared instance would diff one
    camera's frame against a different camera's background, which is not "no
    motion", it is nonsense that happens to produce a number.

    P3: `CrossCameraBatcher` can invoke `_on_batch` for the SAME camera from
    two different threads at once — whichever pump thread happens to fill the
    batch, and separately the flush thread on a deadline. Both would then call
    `should_process` on this same gate concurrently. Without a lock, one
    thread's read of `self._background` at the diff step could race another
    thread's write of it moments later, producing a `changed_fraction` diffed
    against a background frame that was never actually current — and
    `frames_seen`/`frames_passed` increments could race too. `skip_ratio` is
    the number the whole streams-per-GPU argument rests on, so this whole
    method is one critical section, not just the parts that look mutable.
    """

    def __init__(self, settings: DetectorSettings | None = None) -> None:
        self._s = settings or detector_settings()
        self._background: np.ndarray | None = None
        self._epoch: int | None = None
        self._warmup_remaining = 0
        self.frames_seen = 0
        self.frames_passed = 0
        self._lock = threading.Lock()

    def should_process(self, frame: SampledFrame) -> bool:
        """True when `frame` should continue to the vehicle detector."""
        with self._lock:
            self.frames_seen += 1
            timing = frame.timing

            if self._epoch != timing.loop_epoch:
                # The scene behind the old background model no longer exists.
                # Rebuilding from scratch is cheap; diffing against a stale
                # model is the exact failure this gate exists to avoid, on
                # every camera, every loop cycle.
                self._epoch = timing.loop_epoch
                self._background = None
                self._warmup_remaining = self._s.motion_warmup_frames

            gray = self._downscale(frame.image)

            if self._background is None:
                self._background = gray
                self.frames_passed += 1
                return True

            if self._warmup_remaining > 0:
                self._warmup_remaining -= 1
                self._background = gray
                self.frames_passed += 1
                return True

            if timing.replaying:
                # V4 / DAY2-DESIGN.md §4.2: a GOP replay burst delivers
                # buffered frames far faster than real time, so two
                # consecutive samples here can straddle a much bigger (or
                # smaller) stretch of stream time than `sample_fps` implies
                # live. `changed_fraction` against `motion_threshold` is
                # calibrated for the live cadence, so it is not a meaningful
                # reading here -- but the frame is still real pixels, so it
                # goes to detection unconditionally, same as warmup, and the
                # background is kept current for when live delivery resumes.
                self._background = gray
                self.frames_passed += 1
                return True

            diff = cv2.absdiff(gray, self._background)
            _, thresholded = cv2.threshold(diff, _PIXEL_DELTA_THRESHOLD, 255, cv2.THRESH_BINARY)
            dilated = cv2.dilate(thresholded, _DILATE_KERNEL)
            changed_fraction = float(np.count_nonzero(dilated)) / dilated.size

            self._background = cv2.addWeighted(
                gray, _BACKGROUND_ALPHA, self._background, 1.0 - _BACKGROUND_ALPHA, 0.0
            )

            passed = changed_fraction >= self._s.motion_threshold
            if passed:
                self.frames_passed += 1
            return passed

    @staticmethod
    def _downscale(image: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        return cv2.resize(gray, (_GATE_WIDTH, _GATE_HEIGHT), interpolation=cv2.INTER_AREA)

    @property
    def skip_ratio(self) -> float:
        """Fraction of frames this gate has dropped. This number, aggregated
        across every camera, is the streams-per-GPU argument (§4) — not a
        micro-optimisation metric nobody reads."""
        with self._lock:
            frames_seen, frames_passed = self.frames_seen, self.frames_passed
        if frames_seen == 0:
            return 0.0
        return 1.0 - (frames_passed / frames_seen)
