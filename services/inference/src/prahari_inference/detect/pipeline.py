"""Wiring for the cascade: SampleGate has already run by the time a frame gets
here (see `capture.py`); this module is MotionGate → VehicleDetector →
PlateReader → protobuf.

`TamperDetector` is not called from here. It runs on every sampled frame
independently, off the worker's heartbeat path (`worker.py`, owned
separately) — a tamper verdict is a per-frame, per-camera observation with no
dependency on the batched detection path, and gating it behind batch flushes
would delay tamper reporting by up to `batch_timeout_ms` for no benefit.
`DetectionResult.tamper` is therefore always `None` from this pipeline.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import replace

from google.protobuf.timestamp_pb2 import Timestamp
from prahari.v1 import common_pb2, events_pb2
from prahari_common.plates import normalise_plate

from prahari_inference.config import DetectorSettings, detector_settings
from prahari_inference.timing import FrameTiming

from .motion import MotionGate
from .types import (
    DetectionResult,
    PlateCandidate,
    PlateReader,
    SampledFrame,
    VehicleBox,
    VehicleDetector,
)

__all__ = ["DetectionPipeline"]

log = logging.getLogger(__name__)


class DetectionPipeline:
    """Owns per-camera motion state and drives one batch through the cascade.

    A single `MotionGate` shared across cameras would diff one camera's frame
    against a different camera's background model — `CrossCameraBatcher`
    interleaves cameras within a batch by design, so gates are keyed by
    `camera_id` and created lazily on first sight of that camera.

    P3: `CrossCameraBatcher` invokes `_on_batch` (and so `process_batch`) on
    whichever pump thread happens to fill a batch, and separately on the
    flush thread when a deadline fires — so two batches, both containing the
    same camera, can call `_gate_for` concurrently. Without a lock this is a
    plain check-then-act race: both see `None` for that camera, both
    construct a `MotionGate`, and whichever assignment loses is silently
    dropped, along with the background model and frame counters the other
    thread already accumulated against it.
    """

    def __init__(
        self,
        vehicle_detector: VehicleDetector,
        plate_reader: PlateReader,
        settings: DetectorSettings | None = None,
    ) -> None:
        self._s = settings or detector_settings()
        self._vehicle_detector = vehicle_detector
        self._plate_reader = plate_reader
        self._motion_gates: dict[str, MotionGate] = {}
        self._gates_lock = threading.Lock()

    def _gate_for(self, camera_id: str) -> MotionGate:
        with self._gates_lock:
            gate = self._motion_gates.get(camera_id)
            if gate is None:
                gate = MotionGate(self._s)
                self._motion_gates[camera_id] = gate
            return gate

    def process_batch(self, frames: list[SampledFrame]) -> list[DetectionResult]:
        """Run one cross-camera batch through the cascade.

        Returns one `DetectionResult` per input frame, in input order — a
        frame the motion gate drops still produces a result, with
        `motion_skipped=True`, so the skip ratio is measurable per the
        contract on `DetectionResult.motion_skipped`.
        """
        to_detect: list[SampledFrame] = []
        skipped: set[int] = set()

        for index, frame in enumerate(frames):
            if self._s.motion_gate and not self._gate_for(frame.camera_id).should_process(frame):
                skipped.add(index)
            else:
                to_detect.append(frame)

        vehicles_by_frame: dict[int, list[VehicleBox]] = {}
        if to_detect:
            detected = self._vehicle_detector.detect(to_detect)
            for frame, boxes in zip(to_detect, detected, strict=True):
                vehicles_by_frame[id(frame)] = boxes

        results: list[DetectionResult] = []
        for index, frame in enumerate(frames):
            if index in skipped:
                results.append(
                    DetectionResult(
                        camera_id=frame.camera_id, timing=frame.timing, motion_skipped=True
                    )
                )
                continue

            vehicles = vehicles_by_frame.get(id(frame), [])
            plates: list[PlateCandidate] = []
            for vehicle_index, vehicle in enumerate(vehicles):
                candidate = self._plate_reader.read(frame.image, vehicle)
                if candidate is not None:
                    # Stamp the pairing now, while it is exact -- `read()` was
                    # just called against THIS vehicle specifically. Once
                    # `plates` and `vehicles` are separated into
                    # `DetectionResult`'s two flat lists, that association is
                    # otherwise unrecoverable (see `_pair_plates_to_vehicles`).
                    plates.append(replace(candidate, vehicle_index=vehicle_index))

            results.append(
                DetectionResult(
                    camera_id=frame.camera_id,
                    timing=frame.timing,
                    vehicles=vehicles,
                    plates=plates,
                )
            )
        return results

    def to_protobuf(self, result: DetectionResult) -> list[events_pb2.VehicleDetection]:
        """One `VehicleDetection` per detected vehicle in `result`.

        `PlateCandidate.box` is the plate's OWN bounding box, not a
        back-reference — `events.proto`'s `PlateReading.plate_box` needs one
        and `DetectionResult` stores vehicles and plates as two unlinked flat
        lists, so pairing is solved here by geometric containment: a plate
        belongs to the smallest vehicle box (by area) whose bounds contain the
        plate's center. Smallest, not first-containing, because a plate can sit
        inside more than one overlapping vehicle box near an occlusion, and the
        vehicle it is actually mounted on is the tightest-fitting one.
        """
        plate_by_vehicle = _pair_plates_to_vehicles(result.vehicles, result.plates)
        observed_at = _stream_time(result.timing)

        detections = []
        for vehicle in result.vehicles:
            candidate = plate_by_vehicle.get(id(vehicle))
            detections.append(
                events_pb2.VehicleDetection(
                    detection_id=str(uuid.uuid4()),
                    camera_id=result.camera_id,
                    observed_at=observed_at,
                    vehicle_box=_bounding_box(vehicle),
                    vehicle_class=vehicle.vehicle_class,
                    vehicle_confidence=vehicle.confidence,
                    plate=_plate_reading(candidate) if candidate is not None else None,
                )
            )
        return detections


def _pair_plates_to_vehicles(
    vehicles: list[VehicleBox], plates: list[PlateCandidate]
) -> dict[int, PlateCandidate]:
    """Three passes, trust falling off as the evidence gets weaker.

    Pass 1 trusts `plate.vehicle_index` — stamped by `process_batch`, which
    called the reader once per vehicle and therefore knows the pairing
    exactly. This is the only sound path once any vehicle in the batch has no
    legible plate: position within the surviving-plates list is no longer a
    reliable proxy for position in the vehicle list the moment a `None` in
    the middle is dropped.

    Passes 2 and 3 exist only for plates with no index — a caller that builds
    a `DetectionResult` directly and skips `process_batch`, as several tests
    do. Pass 2 pairs plates that carry their own box, by geometric
    containment. Pass 3 pairs the (now rare) unboxed, unindexed remainder
    positionally against whatever pass 1/2 left unclaimed; this is the same
    best-effort guess V1 identified as unsound, kept only as a last resort
    for inputs that predate `vehicle_index`.
    """
    paired: dict[int, PlateCandidate] = {}
    claimed: set[int] = set()
    unindexed: list[PlateCandidate] = []

    for plate in plates:
        if plate.vehicle_index is None:
            unindexed.append(plate)
            continue
        if not 0 <= plate.vehicle_index < len(vehicles):
            log.warning(
                "plate vehicle_index %d out of range for %d vehicles; dropping plate",
                plate.vehicle_index,
                len(vehicles),
            )
            continue
        vehicle = vehicles[plate.vehicle_index]
        if id(vehicle) in claimed:
            log.warning(
                "plate vehicle_index %d already claimed by another plate; dropping plate",
                plate.vehicle_index,
            )
            continue
        paired[id(vehicle)] = plate
        claimed.add(id(vehicle))

    boxed = [p for p in unindexed if p.box is not None]
    unboxed = [p for p in unindexed if p.box is None]

    for plate in boxed:
        assert plate.box is not None  # narrowed by the filter above
        cx = (plate.box.x_min + plate.box.x_max) / 2.0
        cy = (plate.box.y_min + plate.box.y_max) / 2.0

        best: VehicleBox | None = None
        best_area = float("inf")
        for vehicle in vehicles:
            if id(vehicle) in claimed:
                continue
            contains = vehicle.x_min <= cx <= vehicle.x_max and vehicle.y_min <= cy <= vehicle.y_max
            if not contains:
                continue
            area = (vehicle.x_max - vehicle.x_min) * (vehicle.y_max - vehicle.y_min)
            if area < best_area:
                best, best_area = vehicle, area

        if best is not None:
            paired[id(best)] = plate
            claimed.add(id(best))
        else:
            log.warning("boxed plate has no containing vehicle; dropping plate")

    remaining = (v for v in vehicles if id(v) not in claimed)
    for plate in unboxed:
        vehicle = next(remaining, None)
        if vehicle is None:
            break
        paired[id(vehicle)] = plate

    return paired


def _stream_time(timing: FrameTiming) -> common_pb2.StreamTime:
    wall_clock = Timestamp()
    wall_clock.FromMicroseconds(int(timing.wall_clock * 1_000_000))
    return common_pb2.StreamTime(
        pts_ms=int(timing.pts_ms),
        wall_clock=wall_clock,
        loop_epoch=timing.loop_epoch,
    )


def _bounding_box(box: VehicleBox) -> common_pb2.BoundingBox:
    return common_pb2.BoundingBox(
        x_min=box.x_min, y_min=box.y_min, x_max=box.x_max, y_max=box.y_max
    )


def _plate_reading(candidate: PlateCandidate) -> events_pb2.PlateReading:
    """`char_confidence` on the wire stays aligned to `raw_text`, exactly as
    read — NOT run through `project_confidences`, which re-indexes onto the
    *normalised* text and would desynchronise the two the instant a separator
    or an `IND` prefix is stripped. `events.proto` states the raw alignment as
    an invariant; `normalise_plate` is used ONLY for `normalised_text` and
    `format`, both of which are independent, derived fields the match engine
    treats as a convenience, not as the character-confidence contract.
    """
    normalised = normalise_plate(candidate.raw_text)
    reading = events_pb2.PlateReading(
        raw_text=candidate.raw_text,
        char_confidence=list(candidate.char_confidence),
        normalised_text=normalised.text,
        format=int(normalised.format),
    )
    if candidate.box is not None:
        reading.plate_box.CopyFrom(_bounding_box(candidate.box))
    return reading
