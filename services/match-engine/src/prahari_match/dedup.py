"""Alert deduplication (DAY2-DESIGN.md §7.3).

A vehicle in frame for 8 s at 3 fps produces ~24 detections, and without this
module, 24 alerts for the same pass. The key is `(camera_id, matched_plate,
floor(wall_clock / bucket_s))` -- coarse enough that one dwell collapses to
one alert, fine enough that the same plate passing again an hour later is a
new alert, not a suppressed duplicate.

Deliberately in-process and bounded, not Redis-backed: this is a rate-limiter
for the console, not the system of record (the alert itself, once raised, is
durable via the bus). A restart losing the last few seconds of dedup state
just means a handful of possible duplicate alerts, not a correctness bug.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict

__all__ = ["Deduper"]


class Deduper:
    def __init__(self, bucket_s: float, max_entries: int) -> None:
        if bucket_s <= 0:
            raise ValueError("bucket_s must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._bucket_s = bucket_s
        self._max_entries = max_entries
        # Insertion-ordered so eviction can drop the oldest key in O(1) without
        # a separate timestamp index -- `seen` is the only structure needed.
        self._seen: OrderedDict[str, float] = OrderedDict()
        # P4: `MetadataIngestServicer` is served by a
        # `ThreadPoolExecutor(max_workers=settings.grpc_max_workers)`, so
        # `should_alert` runs concurrently across worker streams. Without this
        # lock, two threads racing on the same (camera, plate, bucket) both
        # see `key in self._seen` as False before either assigns, and both
        # return True -- two alerts for one dwell, exactly what this module
        # exists to prevent. `move_to_end`/`popitem` on a plain `OrderedDict`
        # are not thread-safe either, so the whole method body is one
        # critical section, not just the membership check.
        self._lock = threading.Lock()

    def key_for(self, camera_id: str, matched_plate: str, wall_clock_s: float | None = None) -> str:
        wall_clock_s = wall_clock_s if wall_clock_s is not None else time.time()
        bucket = math.floor(wall_clock_s / self._bucket_s)
        return f"{camera_id}:{matched_plate}:{bucket}"

    def should_alert(
        self, camera_id: str, matched_plate: str, wall_clock_s: float | None = None
    ) -> bool:
        """True the first time this (camera, plate, bucket) is seen; False on
        every repeat within the same bucket. Marks the key seen as a side
        effect -- one call per detection, not a peek-then-mark pair, so a
        caller cannot race itself into double-counting the same detection."""
        key = self.key_for(camera_id, matched_plate, wall_clock_s)
        with self._lock:
            if key in self._seen:
                # Touch it for LRU purposes but do not re-alert.
                self._seen.move_to_end(key)
                return False

            self._seen[key] = wall_clock_s if wall_clock_s is not None else time.time()
            if len(self._seen) > self._max_entries:
                # Evict oldest: bounded memory over an unbounded runtime. A
                # worker that runs for a week must not grow a dedup key per
                # sighting.
                self._seen.popitem(last=False)
            return True

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)
