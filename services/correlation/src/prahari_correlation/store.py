"""The detection store (DAY3-DESIGN.md §3.1): an in-memory, bounded index
over every `VehicleDetection` read off `prahari:detections`. Laptop-scale
demo store, not a database -- Day 5's load test is explicitly out of scope
for this store's design; a real deployment would put this behind Timescale
the way heartbeats are. That is a known limitation, not a hidden one.

Two indices, because route reconstruction needs two different questions
answered:

* "every sighting of this plate" -- `by_plate`, keyed by
  `prahari_common.plates.normalise_plate(...).text` so a caller typing
  `GJ 01 AB 1234` and one typing `gj01ab1234` hit the same bucket. **Never
  re-derive plate parsing here** -- CLAUDE.md.
* "every plate-unreadable sighting in this time window" -- `unplated_between`,
  the candidate pool DAY3-DESIGN.md §3.3's appearance bridging searches
  across cameras for a detection that might continue a plate-confirmed
  segment through a gap.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict, deque
from collections.abc import Iterable

from prahari.v1 import events_pb2
from prahari_common.plates import normalise_plate

__all__ = ["DetectionStore", "plate_key", "wall_clock_s"]

log = logging.getLogger(__name__)


def plate_key(raw_text: str) -> str:
    return normalise_plate(raw_text).text


def wall_clock_s(detection: events_pb2.VehicleDetection) -> float:
    """Seconds since the epoch, or `0.0` when unset -- callers that need
    strict chronological ordering are expected to have already discarded
    detections with no usable timestamp; this is a safe, sortable default,
    not a claim that the detection happened at the epoch.

    Read directly off `ts.seconds`/`ts.nanos` rather than through
    `ts.ToDatetime().timestamp()`: `ToDatetime()` returns a *naive* UTC
    datetime, and `.timestamp()` on a naive datetime interprets it in the
    process's local timezone -- silently shifting every timestamp by the
    deployment's UTC offset."""
    ts = detection.observed_at.wall_clock
    if ts.seconds == 0 and ts.nanos == 0:
        return 0.0
    return ts.seconds + ts.nanos / 1e9


class DetectionStore:
    def __init__(
        self,
        max_per_plate: int,
        max_plates: int,
        max_unplated: int = 20_000,
    ) -> None:
        self._max_per_plate = max_per_plate
        self._max_plates = max_plates
        # OrderedDict as an LRU over plate keys: the oldest-touched plate is
        # evicted first when `max_plates` distinct plates have been seen,
        # bounding total memory regardless of how many distinct plates a
        # long-running demo observes.
        self._by_plate: OrderedDict[str, deque[events_pb2.VehicleDetection]] = OrderedDict()
        self._unplated: deque[events_pb2.VehicleDetection] = deque(maxlen=max_unplated)
        # `add()` runs on the background consumer thread (consumer.py);
        # `by_plate`/`unplated_between`/`tracked_plate_count` run on the
        # FastAPI event loop thread, handling a request. Without a lock,
        # `unplated_between`'s generator drained mid-iteration by a
        # concurrent `add()` on the same deque raises
        # `RuntimeError: deque mutated during iteration` -- reproducible
        # under load, not hypothetical.
        self._lock = threading.Lock()

    def add(self, detection: events_pb2.VehicleDetection) -> None:
        if wall_clock_s(detection) == 0.0:
            # No usable timestamp -- cannot be chronologically ordered or
            # feasibility-gated. Silently keeping it would sort it to the
            # front of every plate's history (0.0 predates every real
            # detection) and make every hop through it look trivially
            # feasible (a ~55-year elapsed time drives implied speed to
            # ~0 km/h) -- the opposite of "no timestamp means untrustworthy".
            log.warning(
                "dropping detection %s from camera %s: no usable observed_at.wall_clock",
                detection.detection_id,
                detection.camera_id,
            )
            return
        with self._lock:
            if detection.HasField("plate") and detection.plate.normalised_text:
                self._add_plated(detection)
            else:
                self._unplated.append(detection)

    def _add_plated(self, detection: events_pb2.VehicleDetection) -> None:
        """Caller must hold `self._lock`."""
        key = plate_key(detection.plate.normalised_text)
        if not key:
            # Legible characters that normalised to nothing (e.g. pure
            # separators) are not a usable index key -- keep the sighting as
            # evidence via the unplated pool rather than dropping it.
            self._unplated.append(detection)
            return

        if key in self._by_plate:
            self._by_plate.move_to_end(key)
        else:
            if len(self._by_plate) >= self._max_plates:
                self._by_plate.popitem(last=False)
            self._by_plate[key] = deque(maxlen=self._max_per_plate)
        self._by_plate[key].append(detection)

    def by_plate(self, raw_plate_text: str) -> list[events_pb2.VehicleDetection]:
        """Every stored sighting of `raw_plate_text`'s normalised key,
        oldest first."""
        key = plate_key(raw_plate_text)
        with self._lock:
            snapshot = list(self._by_plate.get(key, ()))
        return sorted(snapshot, key=wall_clock_s)

    def unplated_between(
        self, start_s: float, end_s: float
    ) -> Iterable[events_pb2.VehicleDetection]:
        """Plate-unreadable sightings with `wall_clock` in `[start_s, end_s]`,
        the candidate pool for appearance bridging across a gap between two
        plate-confirmed sightings at `start_s` and `end_s`. Snapshots the
        deque under the lock before filtering, rather than returning a
        generator over the live deque -- see the thread-safety note on
        `self._lock`."""
        with self._lock:
            snapshot = list(self._unplated)
        return [d for d in snapshot if start_s <= wall_clock_s(d) <= end_s]

    def tracked_plate_count(self) -> int:
        with self._lock:
            return len(self._by_plate)
