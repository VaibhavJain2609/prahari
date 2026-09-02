"""Tests for the plate-reading stage.

Only `ScriptedPlateReader` runs here, for the same reason `test_vehicles.py`
only exercises `ScriptedVehicleDetector`: `paddleocr` must not be required by
`make test`, so `PaddlePlateReader`'s only coverage is that importing this
module does not pull it in.
"""

from __future__ import annotations

import ast
from pathlib import Path

import numpy as np

from prahari_inference.detect.plates import PaddlePlateReader, ScriptedPlateReader
from prahari_inference.detect.types import PlateCandidate, VehicleBox

_IMAGE = np.zeros((10, 10, 3), dtype=np.uint8)


class TestScriptedPlateReader:
    def test_returns_scripted_candidate_for_the_exact_vehicle_object(self):
        vehicle = VehicleBox(0.0, 0.0, 1.0, 1.0, "car", 0.9)
        candidate = PlateCandidate(raw_text="GJ01AB1234", char_confidence=(0.9,) * 10)
        reader = ScriptedPlateReader({id(vehicle): candidate})

        assert reader.read(_IMAGE, vehicle) is candidate

    def test_unscripted_vehicle_returns_none(self):
        reader = ScriptedPlateReader()
        vehicle = VehicleBox(0.0, 0.0, 1.0, 1.0, "car", 0.9)

        assert reader.read(_IMAGE, vehicle) is None

    def test_coordinate_identical_vehicles_are_not_confused(self):
        # Two separate VehicleBox instances with the same field values must not
        # collide: identity keying, not value equality, is what the pipeline
        # relies on to keep plates paired to the correct vehicle instance.
        vehicle_a = VehicleBox(0.1, 0.1, 0.5, 0.5, "car", 0.9)
        vehicle_b = VehicleBox(0.1, 0.1, 0.5, 0.5, "car", 0.9)
        candidate = PlateCandidate(raw_text="GJ05CD5678", char_confidence=(0.8,) * 10)
        reader = ScriptedPlateReader({id(vehicle_a): candidate})

        assert reader.read(_IMAGE, vehicle_a) is candidate
        assert reader.read(_IMAGE, vehicle_b) is None

    def test_records_calls(self):
        reader = ScriptedPlateReader()
        vehicle = VehicleBox(0.0, 0.0, 1.0, 1.0, "car", 0.9)

        reader.read(_IMAGE, vehicle)

        assert reader.calls == [vehicle]


class TestPaddleImportDiscipline:
    def test_module_import_does_not_require_paddleocr(self):
        assert PaddlePlateReader is not None

    def test_paddleocr_import_is_lexically_inside_load_not_at_module_scope(self):
        import prahari_inference.detect.plates as mod

        tree = ast.parse(Path(mod.__file__).read_text())
        module_level_imports = {
            alias.name
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        assert "paddleocr" not in module_level_imports
