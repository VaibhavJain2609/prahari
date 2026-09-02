"""Tests for `DetectionPipeline`: cascade wiring and the protobuf conversion.

Both backends are scripted — `ScriptedVehicleDetector` and
`ScriptedPlateReader` — so this suite needs no model weights, per the
contract every stage boundary in `types.py` is built to satisfy.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from prahari_inference.config import DetectorSettings
from prahari_inference.detect.pipeline import DetectionPipeline
from prahari_inference.detect.plates import ScriptedPlateReader
from prahari_inference.detect.types import PlateCandidate, SampledFrame, VehicleBox
from prahari_inference.detect.vehicles import ScriptedVehicleDetector
from prahari_inference.timing import FrameTiming

_HEIGHT, _WIDTH = 90, 160


def _timing(
    *, loop_epoch: int = 0, pts_ms: float = 1234.0, wall_clock: float = 1_700_000_000.5
) -> FrameTiming:
    return FrameTiming(
        pts_ms=pts_ms,
        delta_ms=40.0,
        wall_clock=wall_clock,
        loop_epoch=loop_epoch,
        replaying=False,
        discontinuity=False,
    )


def _image(value: int = 50) -> np.ndarray:
    return np.full((_HEIGHT, _WIDTH, 3), value, dtype=np.uint8)


def _frame(camera_id: str, **timing_kwargs) -> SampledFrame:
    return SampledFrame(camera_id=camera_id, image=_image(), timing=_timing(**timing_kwargs))


def _settings(**overrides) -> DetectorSettings:
    # 0: exactly one unconditional pass (the frame that seeds the background),
    # so a second call against an identical frame already exercises the real
    # steady-state comparison instead of still warming up.
    overrides.setdefault("motion_warmup_frames", 0)
    return DetectorSettings(**overrides)


class TestMotionSkip:
    def test_skipped_frame_produces_a_result_with_no_vehicles_or_plates(self):
        settings = _settings()
        vehicle_detector = ScriptedVehicleDetector({"cam-1": [VehicleBox(0, 0, 1, 1, "car", 0.9)]})
        pipeline = DetectionPipeline(vehicle_detector, ScriptedPlateReader(), settings)

        # Warm the gate, then feed an identical static frame that steady-state
        # motion should drop.
        pipeline.process_batch([_frame("cam-1")])
        results = pipeline.process_batch([_frame("cam-1")])

        assert results[0].motion_skipped is True
        assert results[0].vehicles == []
        assert results[0].plates == []

    def test_skipped_frame_never_reaches_the_vehicle_detector(self):
        settings = _settings()
        vehicle_detector = ScriptedVehicleDetector({"cam-1": [VehicleBox(0, 0, 1, 1, "car", 0.9)]})
        pipeline = DetectionPipeline(vehicle_detector, ScriptedPlateReader(), settings)

        pipeline.process_batch([_frame("cam-1")])
        vehicle_detector.calls.clear()
        pipeline.process_batch([_frame("cam-1")])

        assert vehicle_detector.calls == [], (
            "a motion-skipped frame must not be batched for inference"
        )

    def test_disabling_motion_gate_processes_every_frame(self):
        settings = _settings(motion_gate=False)
        vehicle_detector = ScriptedVehicleDetector({"cam-1": [VehicleBox(0, 0, 1, 1, "car", 0.9)]})
        pipeline = DetectionPipeline(vehicle_detector, ScriptedPlateReader(), settings)

        pipeline.process_batch([_frame("cam-1")])
        results = pipeline.process_batch([_frame("cam-1")])

        assert results[0].motion_skipped is False
        assert len(results[0].vehicles) == 1

    def test_per_camera_motion_gates_do_not_cross_contaminate(self):
        """A cross-camera batch must not diff cam-2's frame against cam-1's
        background model."""
        settings = _settings()
        vehicle_detector = ScriptedVehicleDetector(
            {
                "cam-1": [VehicleBox(0, 0, 1, 1, "car", 0.9)],
                "cam-2": [VehicleBox(0, 0, 1, 1, "car", 0.9)],
            }
        )
        pipeline = DetectionPipeline(vehicle_detector, ScriptedPlateReader(), settings)

        # Warm both gates independently.
        pipeline.process_batch([_frame("cam-1"), _frame("cam-2")])

        # cam-1 stays static (should be skipped); cam-2 changes sharply (should pass).
        changed = _image(50)
        changed[:, _WIDTH // 2 :, :] = 220
        frame_cam1 = SampledFrame(camera_id="cam-1", image=_image(), timing=_timing())
        frame_cam2 = SampledFrame(camera_id="cam-2", image=changed, timing=_timing())

        results = pipeline.process_batch([frame_cam1, frame_cam2])

        by_camera = {r.camera_id: r for r in results}
        assert by_camera["cam-1"].motion_skipped is True
        assert by_camera["cam-2"].motion_skipped is False

    def test_concurrent_gate_lookups_for_the_same_camera_never_construct_two_gates(self):
        # P3: `CrossCameraBatcher` invokes `_on_batch` from whichever pump
        # thread fills a batch and separately from the flush thread, so
        # `_gate_for` can be called for the same camera from multiple
        # threads at once. Before the fix this was check-then-act: both
        # threads could see no gate for "cam-1", both construct one, and
        # whichever assignment lost silently dropped the other's background
        # model and frame counters.
        from concurrent.futures import ThreadPoolExecutor

        settings = _settings()
        vehicle_detector = ScriptedVehicleDetector({"cam-1": [VehicleBox(0, 0, 1, 1, "car", 0.9)]})
        pipeline = DetectionPipeline(vehicle_detector, ScriptedPlateReader(), settings)

        with ThreadPoolExecutor(max_workers=16) as pool:
            gates = list(pool.map(lambda _: pipeline._gate_for("cam-1"), range(64)))

        assert len({id(gate) for gate in gates}) == 1
        assert len(pipeline._motion_gates) == 1


class TestCascadeWiring:
    def test_plates_are_read_for_each_detected_vehicle(self):
        settings = _settings(motion_gate=False)
        vehicle = VehicleBox(0.1, 0.1, 0.5, 0.5, "car", 0.9)
        plate = PlateCandidate(raw_text="GJ01AB1234", char_confidence=(0.9,) * 10)
        vehicle_detector = ScriptedVehicleDetector({"cam-1": [vehicle]})
        plate_reader = ScriptedPlateReader({id(vehicle): plate})
        pipeline = DetectionPipeline(vehicle_detector, plate_reader, settings)

        results = pipeline.process_batch([_frame("cam-1")])

        assert results[0].vehicles == [vehicle]
        # `process_batch` stamps `vehicle_index` on the way out -- it is the
        # only caller with an exact view of which vehicle each plate came from.
        assert results[0].plates == [replace(plate, vehicle_index=0)]

    def test_plate_is_paired_to_the_correct_vehicle_when_an_earlier_one_has_no_plate(self):
        """V1 regression: with more than one vehicle in the batch, a plate must
        pair to the vehicle it was actually read from -- not to whichever
        vehicle happens to be first, which is what a position-in-the-
        surviving-plates-list guess produces the moment an earlier vehicle's
        plate comes back `None`."""
        settings = _settings(motion_gate=False)
        car = VehicleBox(0.0, 0.0, 0.2, 0.2, "car", 0.9)
        bus = VehicleBox(0.3, 0.3, 0.5, 0.5, "bus", 0.9)
        truck = VehicleBox(0.6, 0.6, 0.9, 0.9, "truck", 0.9)
        plate = PlateCandidate(raw_text="GJ01AB1234", char_confidence=(0.9,) * 10)
        vehicle_detector = ScriptedVehicleDetector({"cam-1": [car, bus, truck]})
        # car and bus have no legible plate; only truck's reads successfully.
        plate_reader = ScriptedPlateReader({id(truck): plate})
        pipeline = DetectionPipeline(vehicle_detector, plate_reader, settings)

        result = pipeline.process_batch([_frame("cam-1")])[0]
        detections = {d.vehicle_class: d for d in pipeline.to_protobuf(result)}

        assert not detections["car"].HasField("plate")
        assert not detections["bus"].HasField("plate")
        assert detections["truck"].HasField("plate")
        assert detections["truck"].plate.raw_text == "GJ01AB1234"

    def test_a_vehicle_with_no_legible_plate_still_produces_a_vehicle_result(self):
        settings = _settings(motion_gate=False)
        vehicle = VehicleBox(0.1, 0.1, 0.5, 0.5, "car", 0.9)
        vehicle_detector = ScriptedVehicleDetector({"cam-1": [vehicle]})
        pipeline = DetectionPipeline(vehicle_detector, ScriptedPlateReader(), settings)

        results = pipeline.process_batch([_frame("cam-1")])

        assert results[0].vehicles == [vehicle]
        assert results[0].plates == []

    def test_results_preserve_input_order_across_a_mixed_batch(self):
        settings = _settings(motion_gate=False)
        vehicle_detector = ScriptedVehicleDetector()
        pipeline = DetectionPipeline(vehicle_detector, ScriptedPlateReader(), settings)

        frames = [_frame("cam-1"), _frame("cam-2"), _frame("cam-3")]
        results = pipeline.process_batch(frames)

        assert [r.camera_id for r in results] == ["cam-1", "cam-2", "cam-3"]


class TestProtobufConversion:
    def test_vehicle_fields_map_onto_the_wire_message(self):
        settings = _settings(motion_gate=False)
        vehicle = VehicleBox(0.1, 0.2, 0.6, 0.8, "truck", 0.77)
        pipeline = DetectionPipeline(ScriptedVehicleDetector(), ScriptedPlateReader(), settings)
        result = pipeline.process_batch([_frame("cam-9")])[0]
        result.vehicles.append(
            vehicle
        )  # DetectionResult is a plain dataclass; simplest for this test

        detections = pipeline.to_protobuf(result)

        assert len(detections) == 1
        wire = detections[0]
        assert wire.camera_id == "cam-9"
        assert wire.vehicle_class == "truck"
        assert wire.vehicle_confidence == pytest.approx(0.77)
        assert wire.vehicle_box.x_min == pytest.approx(0.1)
        assert wire.vehicle_box.y_max == pytest.approx(0.8)
        assert wire.HasField("plate") is False

    def test_char_confidence_on_the_wire_is_raw_not_projected(self):
        """INVARIANT (events.proto): char_confidence stays aligned to raw_text,
        exactly as OCR produced it — never re-indexed onto normalised_text."""
        settings = _settings(motion_gate=False)
        vehicle = VehicleBox(0.0, 0.0, 1.0, 1.0, "car", 0.9)
        raw_confidence = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.99)
        plate = PlateCandidate(raw_text="GJ-01AB1234", char_confidence=raw_confidence + (0.5,))
        pipeline = DetectionPipeline(ScriptedVehicleDetector(), ScriptedPlateReader(), settings)
        result = pipeline.process_batch([_frame("cam-1")])[0]
        result.vehicles.append(vehicle)
        result.plates.append(plate)

        wire = pipeline.to_protobuf(result)[0]

        assert wire.plate.raw_text == "GJ-01AB1234"
        # pytest.approx: PlateReading.char_confidence is a proto `float` (32-bit),
        # so exact equality against the float64 tuple above is not meaningful —
        # only that the values, and their alignment to raw_text, survived.
        assert list(wire.plate.char_confidence) == pytest.approx(list(plate.char_confidence))
        assert wire.plate.normalised_text == "GJ01AB1234"

    def test_plate_pairs_to_the_smallest_containing_vehicle(self):
        settings = _settings(motion_gate=False)
        big = VehicleBox(0.0, 0.0, 1.0, 1.0, "bus", 0.9)
        small = VehicleBox(0.4, 0.4, 0.6, 0.6, "car", 0.9)
        plate_box = VehicleBox(0.45, 0.45, 0.55, 0.55, "plate", 0.9)
        plate = PlateCandidate(raw_text="GJ01AB1234", char_confidence=(), box=plate_box)

        pipeline = DetectionPipeline(ScriptedVehicleDetector(), ScriptedPlateReader(), settings)
        result = pipeline.process_batch([_frame("cam-1")])[0]
        result.vehicles.extend([big, small])
        result.plates.append(plate)

        detections = {d.vehicle_class: d for d in pipeline.to_protobuf(result)}

        assert detections["car"].HasField("plate")
        assert not detections["bus"].HasField("plate")

    def test_observed_at_carries_pts_and_loop_epoch(self):
        settings = _settings(motion_gate=False)
        pipeline = DetectionPipeline(ScriptedVehicleDetector(), ScriptedPlateReader(), settings)
        frame = SampledFrame(
            camera_id="cam-1", image=_image(), timing=_timing(loop_epoch=3, pts_ms=9999.0)
        )
        result = pipeline.process_batch([frame])[0]
        result.vehicles.append(VehicleBox(0, 0, 1, 1, "car", 0.9))

        wire = pipeline.to_protobuf(result)[0]

        assert wire.observed_at.pts_ms == 9999
        assert wire.observed_at.loop_epoch == 3
