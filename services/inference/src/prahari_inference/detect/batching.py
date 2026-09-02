"""Cross-camera batching.

A single camera samples at `IngestSettings.sample_fps` (2.0 by default) — one
frame every 500ms. Waiting for one camera alone to fill a `batch_size=4` batch
adds up to 2 seconds of latency before inference even starts, which does not
fit inside a 5-second end-to-end alert budget. So the batch is filled ACROSS
every camera's pump thread instead: eight active cameras at 2 fps produce a
full batch in tens of milliseconds, no matter how quiet any one of them is.

The flush is a race between two triggers, whichever fires first:
- `batch_size` frames have accumulated, or
- `batch_timeout_ms` has elapsed since the oldest frame in the current batch
  arrived.

Without the timeout, a quiet estate (fewer active cameras than `batch_size`)
holds frames indefinitely waiting for a batch that may never fill, and the
alert budget is missed by an unbounded margin instead of a bounded one.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from prahari_inference.config import DetectorSettings, detector_settings

from .types import SampledFrame

__all__ = ["CrossCameraBatcher"]

log = logging.getLogger(__name__)


class CrossCameraBatcher:
    """Thread-safe accumulator fed by every camera's pump thread.

    `submit()` is called from N concurrent pump threads (one per active
    camera); a background flush thread enforces the timeout side of the race
    so a quiet batch does not wait forever for `submit()` calls that are not
    coming.
    """

    def __init__(
        self,
        on_batch: Callable[[list[SampledFrame]], None],
        settings: DetectorSettings | None = None,
    ) -> None:
        self._s = settings or detector_settings()
        self._on_batch = on_batch
        self._lock = threading.Lock()
        self._pending: list[SampledFrame] = []
        self._oldest_arrival: float | None = None
        self._stop = threading.Event()
        self._flush_thread = threading.Thread(
            target=self._flush_loop, name="batch-flush", daemon=True
        )
        self._flush_thread.start()

    def submit(self, frame: SampledFrame) -> None:
        """Add one sampled frame from any camera's pump thread.

        Flushes synchronously, on the submitting thread, when this call fills
        the batch — the size trigger does not wait for the flush thread's next
        tick, which would add up to a full tick of avoidable latency on every
        full batch.
        """
        batch: list[SampledFrame] | None = None
        with self._lock:
            self._pending.append(frame)
            if self._oldest_arrival is None:
                self._oldest_arrival = time.monotonic()
            if len(self._pending) >= self._s.batch_size:
                batch = self._pending
                self._pending = []
                self._oldest_arrival = None

        if batch is not None:
            self._on_batch(batch)

    def _flush_loop(self) -> None:
        # Polls at a fraction of the timeout rather than sleeping the full
        # timeout: sleeping the full window would let a batch sit up to
        # `batch_timeout_ms` PAST its deadline in the worst case, doubling the
        # bound this loop exists to enforce.
        poll_interval_s = max(self._s.batch_timeout_ms / 1000.0 / 4, 0.01)
        while not self._stop.wait(poll_interval_s):
            try:
                self._flush_if_overdue()
            except Exception:
                # P1/V3: `_on_batch` runs the whole cascade (process_batch ->
                # YOLO -> OCR) on THIS thread. An unhandled exception here
                # used to propagate out of the loop and kill this daemon
                # thread silently -- `close()` still joined cleanly and
                # `submit()` kept working, so only the size trigger survived.
                # On a quiet estate (active cameras < batch_size) that means
                # frames accumulate in `_pending` forever, missing the 5s
                # alert budget by an unbounded margin, with no log, no
                # counter, no liveness signal. Log and keep the loop alive --
                # a batch that failed once is not a reason to stop enforcing
                # the deadline for every batch after it.
                log.exception("batch-flush loop: unhandled exception in _flush_if_overdue")

    def _flush_if_overdue(self) -> None:
        batch: list[SampledFrame] | None = None
        with self._lock:
            if self._pending and self._oldest_arrival is not None:
                age_ms = (time.monotonic() - self._oldest_arrival) * 1000.0
                if age_ms >= self._s.batch_timeout_ms:
                    batch = self._pending
                    self._pending = []
                    self._oldest_arrival = None

        if batch is not None:
            self._on_batch(batch)

    def flush(self) -> None:
        """Force whatever is pending out immediately. For shutdown and tests —
        the production path always resolves via size or timeout."""
        batch: list[SampledFrame] | None = None
        with self._lock:
            if self._pending:
                batch = self._pending
                self._pending = []
                self._oldest_arrival = None

        if batch is not None:
            self._on_batch(batch)

    def close(self) -> None:
        """Stop the flush thread and drain whatever is still pending.

        P2: the previous version set `_stop` and joined without ever calling
        `flush()`, silently discarding up to `batch_size - 1` sampled frames
        on every shutdown. Stop and join first, then flush -- flushing before
        the flush thread has stopped would just mean both could observe an
        empty `_pending` and do nothing, which is harmless but redundant; the
        real risk this order avoids is flushing before the LAST timeout-driven
        tick has had a chance to run, which draining again afterwards covers
        either way.

        The join result is also no longer ignored: a `join(timeout=1.0)` that
        times out used to be indistinguishable from a clean stop.
        """
        self._stop.set()
        self._flush_thread.join(timeout=1.0)
        if self._flush_thread.is_alive():
            log.warning(
                "batch-flush thread did not stop within 1.0s during close(); "
                "it is a daemon thread and will be killed with the process, "
                "but may still be mid-flush right now"
            )
        self.flush()
