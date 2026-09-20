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
    def _install_fake_ultralytics(monkeypatch, load_delay_s: float = 0.05) -> type:
        class FakeYOLO:
            loads = 0
            predict_kwargs: dict | None = None

            def __init__(self, weights: str) -> None:
                type(self).loads += 1
                time.sleep(load_delay_s)  # widen the check-then-act window

            def predict(self, images, **kwargs):
                type(self).predict_kwargs = kwargs
                return [SimpleNamespace(boxes=[]) for _ in images]

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
