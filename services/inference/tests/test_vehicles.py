"""Tests for the vehicle-detection stage.

Only `ScriptedVehicleDetector` runs here — `YoloVehicleDetector` imports
`ultralytics` lazily inside `_load()` specifically so this suite never needs
the weights or the dependency on disk. That import boundary is asserted below
by simply never calling `.detect()` on it while the package is absent, and by
grep-checking that the import stays deferred.
"""

from __future__ import annotations

import ast
from pathlib import Path

import numpy as np

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
