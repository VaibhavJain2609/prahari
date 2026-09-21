"""Tests for the vehicle-detection stage.

Only `ScriptedVehicleDetector` runs here — `YoloVehicleDetector` imports
`ultralytics` lazily inside `_load()` specifically so this suite never needs
the weights or the dependency on disk. That import boundary is asserted below
by simply never calling `.detect()` on it while the package is absent, and by
grep-checking that the import stays deferred.
"""

from __future__ import annotations

import ast
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np

from prahari_inference.config import DetectorSettings
from prahari_inference.detect.types import SampledFrame, VehicleBox
from prahari_inference.detect.vehicles import ScriptedVehicleDetector, YoloVehicleDetector
from prahari_inference.timing import FrameTiming


def _timing() -> FrameTiming:
    return FrameTiming(
        pts_ms=0.0,
        delta_ms=40.0,
        wall_clock=1000.0,
        loop_epoch=0,
        replaying=False,
        discontinuity=False,
    )


def _frame(camera_id: str) -> SampledFrame:
    return SampledFrame(
        camera_id=camera_id, image=np.zeros((10, 10, 3), dtype=np.uint8), timing=_timing()
    )


class TestScriptedVehicleDetector:
    def test_returns_scripted_boxes_keyed_by_camera(self):
        box = VehicleBox(0.1, 0.1, 0.5, 0.5, "car", 0.9)
        detector = ScriptedVehicleDetector({"cam-1": [box]})

        result = detector.detect([_frame("cam-1"), _frame("cam-2")])

        assert result == [[box], []]

    def test_records_calls_for_batching_assertions(self):
        detector = ScriptedVehicleDetector()
        frames = [_frame("cam-1"), _frame("cam-2")]

        detector.detect(frames)

        assert detector.calls == [frames]

    def test_empty_script_returns_empty_lists_not_none(self):
        detector = ScriptedVehicleDetector()
        result = detector.detect([_frame("cam-1")])
        assert result == [[]]


class TestYoloLoadOnceAndDevice:
    """`YoloVehicleDetector._load` raced before the lock: `detect()` runs on
    whichever pump thread fills a batch AND on the batcher's flush thread, so
    a cold start could construct the model twice. A fake `ultralytics` module
    stands in — the real one is deliberately absent from the test env."""

    @staticmethod
    def _install_fake_ultralytics(
        monkeypatch, load_delay_s: float = 0.05, boxes_per_image: list | None = None
    ) -> type:
        class FakeYOLO:
            loads = 0
            predict_kwargs: dict | None = None

            def __init__(self, weights: str) -> None:
                type(self).loads += 1
                time.sleep(load_delay_s)  # widen the check-then-act window

            def predict(self, images, **kwargs):
                type(self).predict_kwargs = kwargs
                return [SimpleNamespace(boxes=boxes_per_image or []) for _ in images]

        fake = ModuleType("ultralytics")
        fake.YOLO = FakeYOLO
        monkeypatch.setitem(sys.modules, "ultralytics", fake)
        return FakeYOLO

    def test_concurrent_detect_calls_load_the_model_exactly_once(self, monkeypatch):
        fake_yolo = self._install_fake_ultralytics(monkeypatch)
        detector = YoloVehicleDetector(DetectorSettings())

        threads = [
            threading.Thread(target=detector.detect, args=([_frame("cam-1")],)) for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
            assert not t.is_alive()

        assert fake_yolo.loads == 1, (
            f"_load() ran {fake_yolo.loads} times under concurrency — "
            "check-then-act without the lock"
        )

    def test_predict_receives_the_device_from_settings_not_autodetect(self, monkeypatch):
        """Omitting `device=` lets ultralytics silently pick CUDA — backend
        sniffing, which the profile invariant forbids. The value must be
        whatever the settings say, verbatim."""
        fake_yolo = self._install_fake_ultralytics(monkeypatch, load_delay_s=0.0)
        detector = YoloVehicleDetector(DetectorSettings(device="mps"))

        detector.detect([_frame("cam-1")])

        assert fake_yolo.predict_kwargs is not None
        assert fake_yolo.predict_kwargs["device"] == "mps"

    def test_empty_frame_list_short_circuits_before_loading_weights(self, monkeypatch):
        # An empty batch must not pay the multi-second weights load — detect
        # returns [] without ever calling _load().
        fake_yolo = self._install_fake_ultralytics(monkeypatch, load_delay_s=0.0)
        detector = YoloVehicleDetector(DetectorSettings())

        assert detector.detect([]) == []
        assert fake_yolo.loads == 0

    def test_detect_maps_result_boxes_to_normalised_vehicle_boxes(self, monkeypatch):
        # Pixel xyxy in, normalised [0,1] VehicleBox out — the contract every
        # later stage relies on. A class outside _VEHICLE_CLASSES lands as the
        # generic "vehicle" label, not a KeyError.
        boxes = [
            SimpleNamespace(xyxy=[[1.0, 2.0, 5.0, 6.0]], cls=[2], conf=[0.9]),  # car
            SimpleNamespace(xyxy=[[0.0, 0.0, 10.0, 10.0]], cls=[99], conf=[0.5]),
        ]
        self._install_fake_ultralytics(monkeypatch, load_delay_s=0.0, boxes_per_image=boxes)
        detector = YoloVehicleDetector(DetectorSettings())

        (result,) = detector.detect([_frame("cam-1")])

        assert len(result) == 2
        car, unknown = result
        assert (car.x_min, car.y_min, car.x_max, car.y_max) == (0.1, 0.2, 0.5, 0.6)
        assert car.vehicle_class == "car" and car.confidence == 0.9
        assert unknown.vehicle_class == "vehicle" and unknown.confidence == 0.5

    def test_predict_filters_to_vehicle_classes_inside_the_model(self, monkeypatch):
        # `classes=` must carry exactly _VEHICLE_CLASSES' ids — filtering
        # after the model would waste confidence/NMS budget on persons and
        # traffic lights.
        fake_yolo = self._install_fake_ultralytics(monkeypatch, load_delay_s=0.0)
        detector = YoloVehicleDetector(DetectorSettings(vehicle_confidence=0.4))

        detector.detect([_frame("cam-1")])

        assert sorted(fake_yolo.predict_kwargs["classes"]) == [2, 3, 5, 7]
        assert fake_yolo.predict_kwargs["conf"] == 0.4


class TestYoloImportDiscipline:
    """`ultralytics` is a multi-gigabyte, CUDA-aware dependency. If importing
    this module pulled it in, `make test` would need it installed — exactly
    what `types.py` says every backend must avoid."""

    def test_module_import_does_not_require_ultralytics(self):
        # Reaching this line at all is the assertion: the top-of-file import
        # of YoloVehicleDetector already happened without ultralytics present
        # in the test environment.
        assert YoloVehicleDetector is not None

    def test_ultralytics_import_is_lexically_inside_load_not_at_module_scope(self):
        import prahari_inference.detect.vehicles as mod

        tree = ast.parse(Path(mod.__file__).read_text())
        module_level_imports = {
            alias.name
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        assert "ultralytics" not in module_level_imports
