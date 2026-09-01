"""RTSP capture with supervised reconnect.

Implements §3 of the integrator's guide as executable behaviour:

* RTSP forced over TCP (structurally — see rtsp_env).
* All timing from PTS (delegated to PTSClock).
* Decoder warnings at join are tolerated, not fatal.
* Inter-frame gaps are tolerated and are not disconnects.
* Reconnect with exponential backoff, ~2 s start, ~30 s cap.
* Captures are closed when finished, because every client gets its own copy of
  the stream.

Consume-only: nothing in this module writes to the gateway.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Iterator
from dataclasses import dataclass
from types import TracebackType

import numpy as np

from .catalogue import CameraEntry
from .config import GatewaySettings, IngestSettings, gateway_settings, ingest_settings
from .rtsp_env import capture_options, cv2
from .timing import FrameTiming, PTSClock

log = logging.getLogger(__name__)

# A connection must survive this long before we consider it healthy and reset
# the backoff. Without it, a feed that accepts a connection and immediately
# drops resets the backoff on every attempt and we hammer it at 2 s forever —
# the tight reconnect loop §3 explicitly warns against, wearing a disguise.
_HEALTHY_CONNECTION_S = 60.0

_READ_FAILURE_SLEEP_S = 0.05


@dataclass(frozen=True)
class Frame:
    image: np.ndarray
    timing: FrameTiming
    camera_id: str


class StreamCapture:
    """A supervised capture for one camera.

    Iterating `frames()` yields decoded frames and transparently reconnects.
    The iterator does not terminate on connection loss — that is the point. It
    terminates when the caller stops consuming or calls `close()`.
    """

    def __init__(
        self,
        camera: CameraEntry,
        *,
        gateway: GatewaySettings | None = None,
        ingest: IngestSettings | None = None,
        use_hls: bool = False,
    ) -> None:
        self.camera = camera
        self._g = gateway or gateway_settings()
        self._i = ingest or ingest_settings()
        self._use_hls = use_hls
        self._cap: cv2.VideoCapture | None = None
        self._clock = PTSClock()
        self._closed = False
        self._backoff_s = self._i.backoff_initial_s

    @property
    def url(self) -> str:
        """HLS is the documented fallback when port 8554 is blocked. It is a
        genuine fallback, not a preference: HLS segments add several seconds of
        latency, which the alerting path pays for directly."""
        return (
            self.camera.hls_url(self._g) if self._use_hls else self.camera.rtsp_url(self._g)
        )

    def __enter__(self) -> StreamCapture:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._closed = True
        self._release()

    def _release(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def _connect(self) -> bool:
        self._release()
        log.info(
            "connecting camera=%s url=%s codec=%s opts=%s",
            self.camera.id,
            self.url,
            self.camera.properties.codec or "unknown",
            capture_options(),
        )
        cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            cap.release()
            return False
        self._cap = cap
        # NOTE: CAP_PROP_FPS is deliberately never read here. The declared rate
        # does not match delivery; PTSClock.measured_fps is the real figure.
        return True

    def _sleep_backoff(self) -> None:
        """Exponential backoff with full jitter.

        Jitter is not decoration. All workers reconnect when a supervised feed
        restarts, and un-jittered backoff synchronises them into a thundering
        herd against a gateway that is already unhealthy.
        """
        delay = random.uniform(0.0, self._backoff_s)
        log.warning(
            "camera=%s reconnecting in %.1fs (backoff ceiling %.1fs)",
            self.camera.id,
            delay,
            self._backoff_s,
        )
        time.sleep(delay)
        self._backoff_s = min(self._backoff_s * 2.0, self._i.backoff_max_s)

    def frames(self) -> Iterator[Frame]:
        """Yield decoded frames, reconnecting as needed."""
        if not self.camera.live:
            # Opening a capture against a camera the catalogue reports as down
            # spends a connection slot on a feed that cannot deliver.
            log.warning("camera=%s not live per catalogue; not connecting", self.camera.id)
            return

        while not self._closed:
            if not self._connect():
                self._sleep_backoff()
                continue

            connected_at = time.monotonic()
            consecutive_failures = 0

            while not self._closed:
                assert self._cap is not None
                ok, image = self._cap.read()

                if not ok:
                    consecutive_failures += 1
                    in_grace = (time.monotonic() - connected_at) < self._i.join_grace_s

                    # Attaching mid-stream produces decoder errors until the
                    # first IDR arrives. These self-correct. Aborting here is
                    # exactly the mistake that makes a pipeline "bounce on those
                    # streams" — so during the grace window we simply wait.
                    if in_grace or consecutive_failures < self._i.read_failure_threshold:
                        time.sleep(_READ_FAILURE_SLEEP_S)
                        continue

                    log.warning(
                        "camera=%s read failed %d consecutive times; reconnecting",
                        self.camera.id,
                        consecutive_failures,
                    )
                    break

                consecutive_failures = 0

                if time.monotonic() - connected_at >= _HEALTHY_CONNECTION_S:
                    self._backoff_s = self._i.backoff_initial_s

                timing = self._clock.observe(self._cap.get(cv2.CAP_PROP_POS_MSEC))
                if timing.discontinuity:
                    log.info(
                        "camera=%s discontinuity: loop_epoch=%d — resetting tracker state",
                        self.camera.id,
                        timing.loop_epoch,
                    )
                yield Frame(image=image, timing=timing, camera_id=self.camera.id)

            self._release()
            if not self._closed:
                # A reconnect replays a fresh GOP and may restart PTS, so
                # downstream state is invalid across it — same contract as a
                # loop cut.
                self._clock.mark_reconnect()
                self._sleep_backoff()

    @property
    def measured_fps(self) -> float | None:
        return self._clock.measured_fps


class SampleGate:
    """Decimates a frame stream to a target inference rate, using PTS.

    Decode runs at full rate — you cannot skip frames you have not decoded — but
    detection runs at `sample_fps`. This is the main cost lever in the pipeline.

    Gating on PTS rather than wall clock matters during the GOP replay burst:
    a wall-clock gate would pass the entire burst through to the detector as if
    it were seconds of distinct footage, spiking load at every reconnect.
    """

    def __init__(self, sample_fps: float) -> None:
        if sample_fps <= 0:
            raise ValueError("sample_fps must be positive")
        self._interval_ms = 1000.0 / sample_fps
        self._last_emitted_pts: float | None = None
        self._last_epoch: int | None = None

    def should_process(self, timing: FrameTiming) -> bool:
        if self._last_epoch != timing.loop_epoch:
            # New segment: emit immediately so the detector sees the new scene
            # without waiting out a sampling interval.
            self._last_epoch = timing.loop_epoch
            self._last_emitted_pts = timing.pts_ms
            return True
        if self._last_emitted_pts is None:
            self._last_emitted_pts = timing.pts_ms
            return True
        if timing.pts_ms - self._last_emitted_pts >= self._interval_ms:
            self._last_emitted_pts = timing.pts_ms
            return True
        return False
