"""The detection store (DAY3-DESIGN.md §3.1): an in-memory, bounded index
over every `VehicleDetection` read off `prahari:detections`. Laptop-scale
demo store, not a database -- Day 5's load test is explicitly out of scope
for this store's design; a real deployment would put this behind Timescale
the way heartbeats are. That is a known limitation, not a hidden one.

Two indices, because route reconstruction needs two different questions
answered:

* "every sighting of this plate" -- `by_plate`, keyed by the *confusion
  skeleton* of the normalised text (`prahari_common.plates.skeleton`), not
  the normalised text itself. Keying on raw normalised text fragments one
  vehicle into two routes on a single OCR confusion: `GJ01AB1234` and
  `GJ01AB1Z34` are the same plate read twice, and exact-key bucketing makes
  each its own route. The skeleton folds confusable glyphs onto one identity
  key; the raw `normalised_text` stays on each stored detection untouched,
  so display and evidence never see the folded form. **Never re-derive
  plate parsing or canonicalisation here** -- CLAUDE.md.
* "every plate-unreadable sighting in this time window" -- `unplated_between`,
  the candidate pool DAY3-DESIGN.md §3.3's appearance bridging searches
  across cameras for a detection that might continue a plate-confirmed
  segment through a gap.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Iterable

from prahari.v1 import events_pb2
from prahari_common.plates import normalise_plate, skeleton

from .metrics import Metrics

__all__ = ["DetectionStore", "plate_key", "wall_clock_s"]

log = logging.getLogger(__name__)


def plate_key(raw_text: str) -> str:
    """The store's index key: the confusion skeleton of the normalised text.

    Two OCR readings of one plate that differ by a known confusion (`2`/`Z`,
    `0`/`O`, `5`/`S`, ...) land on the same key. A query typed as
    `GJ 01 AB 1234` and a detection whose `normalised_text` is `GJ01AB1Z34`
    therefore share a bucket -- which is the whole point: the index key
    answers "which vehicle", not "which string"."""
    return skeleton(normalise_plate(raw_text).text)


def _dedup_key(detection: events_pb2.VehicleDetection) -> str:
    """What identifies "this exact detection, possibly replayed". Prefers
    `detection_id` -- the publisher-assigned identity, stable across XADD
    retries and consumer restarts. When it is absent (an upstream that did
    not set it), falls back to `(camera_id, pts_ms, raw_text)`: weaker, but
    replayed copies carry identical values for all three."""
    if detection.detection_id:
        return f"id:{detection.detection_id}"
    raw = detection.plate.raw_text if detection.HasField("plate") else ""
    return f"fallback:{detection.camera_id}:{detection.observed_at.pts_ms}:{raw}"


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
        *,
        future_skew_allowance_s: float = 5.0,
        max_seen_ids: int = 100_000,
        metrics: Metrics | None = None,
    ) -> None:
        self._max_per_plate = max_per_plate
        self._max_plates = max_plates
        self._future_skew_allowance_s = future_skew_allowance_s
        self._max_seen_ids = max_seen_ids
        self._metrics = metrics if metrics is not None else Metrics()
        # OrderedDict as an LRU over plate keys: the oldest-touched plate is
        # evicted first when `max_plates` distinct plates have been seen,
        # bounding total memory regardless of how many distinct plates a
        # long-running demo observes.
        self._by_plate: OrderedDict[str, deque[events_pb2.VehicleDetection]] = OrderedDict()
        self._unplated: deque[events_pb2.VehicleDetection] = deque(maxlen=max_unplated)
        # Bounded FIFO over dedup keys (`_dedup_key`): XADD retries and
        # consumer restarts can replay a detection that was already stored,
        # and without this every replay lands as a second sighting of the
        # same instant. FIFO eviction (not LRU) -- a replay arrives shortly
        # after the original, so recency-of-first-seen is the right order.
        self._seen_ids: OrderedDict[str, None] = OrderedDict()
        # `add()` runs on the background consumer thread (consumer.py);
        # `by_plate`/`unplated_between`/`tracked_plate_count` run on the
        # FastAPI event loop thread, handling a request. Without a lock,
        # `unplated_between`'s generator drained mid-iteration by a
        # concurrent `add()` on the same deque raises
        # `RuntimeError: deque mutated during iteration` -- reproducible
        # under load, not hypothetical.
        self._lock = threading.Lock()

    def add(self, detection: events_pb2.VehicleDetection) -> None:
        self._metrics.inc("detections_consumed")
        with self._lock:
            key = _dedup_key(detection)
            if key in self._seen_ids:
                # A replay (XADD retry, consumer restart) is normal traffic,
                # not an anomaly worth warning about -- but it is counted, so
                # a replay *storm* still shows up in /metrics.
                self._metrics.inc("detections_dropped_duplicate")
                log.debug("dropping replayed detection %s", key)
                return
            if len(self._seen_ids) >= self._max_seen_ids:
                self._seen_ids.popitem(last=False)
            self._seen_ids[key] = None

            observed = wall_clock_s(detection)
            if observed == 0.0:
                # No usable timestamp -- cannot be chronologically ordered or
                # feasibility-gated. Silently keeping it would sort it to the
                # front of every plate's history (0.0 predates every real
                # detection) and make every hop through it look trivially
                # feasible (a ~55-year elapsed time drives implied speed to
                # ~0 km/h) -- the opposite of "no timestamp means
                # untrustworthy".
                self._metrics.inc("detections_dropped_no_timestamp")
                log.warning(
                    "dropping detection %s from camera %s: no usable observed_at.wall_clock",
                    detection.detection_id,
                    detection.camera_id,
                )
                return
            if observed > time.time() + self._future_skew_allowance_s:
                # A timestamp in the future is poisoned data, not a prediction:
                # indexing it would pin it at the END of the plate's history,
                # where every real subsequent sighting becomes a
                # negative-elapsed hop. The allowance matches the clock-skew
                # bound feasibility gating uses (`clock_skew_allowance_s`),
                # so a merely mis-synced camera is not dropped.
                self._metrics.inc("detections_dropped_future_timestamp")
                log.warning(
                    "dropping detection %s from camera %s: observed_at.wall_clock "
                    "%.3f is %.3fs in the future (allowance %.3fs)",
                    detection.detection_id,
                    detection.camera_id,
                    observed,
                    observed - time.time(),
                    self._future_skew_allowance_s,
                )
                return
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

    def unplated_count(self) -> int:
        with self._lock:
            return len(self._unplated)

    def tracked_plate_count(self) -> int:
        with self._lock:
            return len(self._by_plate)
