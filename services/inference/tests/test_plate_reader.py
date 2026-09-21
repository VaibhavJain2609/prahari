"""Tests for the plate-reading stage.

Only `ScriptedPlateReader` runs here, for the same reason `test_vehicles.py`
only exercises `ScriptedVehicleDetector`: `paddleocr` must not be required by
`make test`, so `PaddlePlateReader`'s only coverage is that importing this
module does not pull it in.
"""

from __future__ import annotations

import ast
import sys
import threading
import time
from pathlib import Path
from types import ModuleType

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


def _install_fake_paddleocr(monkeypatch, result):
    """A `paddleocr` module whose PaddleOCR.ocr() returns a canned result —
    the documented nested per-image/per-line shape
    `[[ [box, (text, confidence)], ... ]]`."""

    class FakePaddleOCR:
        def __init__(self, **_kwargs) -> None:
            pass

        def ocr(self, _crop, cls=True):  # noqa: ANN001, ANN202
            return result

    fake = ModuleType("paddleocr")
    fake.PaddleOCR = FakePaddleOCR
    monkeypatch.setitem(sys.modules, "paddleocr", fake)
    return FakePaddleOCR


class TestPaddlePlateReaderRead:
    """`read()` is exercisable without weights — paddleocr only needs to exist
    as a module for `_load` to import, so a scripted stand-in exercises the
    whole crop → OCR → confidence-gate path."""

    def test_returns_none_when_the_crop_is_empty(self, monkeypatch) -> None:
        # Vehicle box entirely outside the frame: the slice is empty and OCR
        # must not run at all.
        _install_fake_paddleocr(monkeypatch, "unreachable")
        reader = PaddlePlateReader()
        vehicle = VehicleBox(2.0, 2.0, 3.0, 3.0, "car", 0.9)

        assert reader.read(_IMAGE, vehicle) is None
        assert reader._ocr is None  # noqa: SLF001 -- _load never ran

    def test_reads_the_highest_confidence_line_and_broadcasts_it(self, monkeypatch) -> None:
        # A plate crop can contain a second line of text (dealer frame,
        # bumper sticker): the highest-confidence line wins, and the line
        # confidence is broadcast per-character — honest about what the model
        # measured, which is what the matcher's substitution pricing needs.
        _install_fake_paddleocr(
            monkeypatch,
            [
                [
                    ([[0, 0], [1, 0]], ("DEALER", 0.40)),
                    ([[0, 1], [1, 1]], ("GJ01AB1234", 0.90)),
                ]
            ],
        )
        reader = PaddlePlateReader()
        vehicle = VehicleBox(0.0, 0.0, 1.0, 1.0, "car", 0.9)

        candidate = reader.read(_IMAGE, vehicle)

        assert candidate is not None
        assert candidate.raw_text == "GJ01AB1234"
        assert candidate.char_confidence == (0.90,) * len("GJ01AB1234")

    def test_a_line_below_the_confidence_floor_returns_none(self, monkeypatch) -> None:
        from prahari_inference.config import DetectorSettings

        _install_fake_paddleocr(monkeypatch, [[([[0, 0]], ("X", 0.10))]])
        reader = PaddlePlateReader(DetectorSettings(plate_confidence=0.95))
        vehicle = VehicleBox(0.0, 0.0, 1.0, 1.0, "car", 0.9)

        assert reader.read(_IMAGE, vehicle) is None

    def test_no_legible_line_returns_none(self, monkeypatch) -> None:
        _install_fake_paddleocr(monkeypatch, [[]])  # page exists, no lines
        reader = PaddlePlateReader()
        vehicle = VehicleBox(0.0, 0.0, 1.0, 1.0, "car", 0.9)

        assert reader.read(_IMAGE, vehicle) is None


class TestPaddleLoadOnce:
    """Same cold-start race as `YoloVehicleDetector._load`: `read()` runs on
    whichever thread the batch landed on, so the check-then-act needs the
    lock or two threads build PaddleOCR twice."""

    def test_concurrent_reads_load_the_ocr_exactly_once(self, monkeypatch):
        class FakePaddleOCR:
            loads = 0

            def __init__(self, **_kwargs) -> None:
                type(self).loads += 1
                time.sleep(0.05)  # widen the check-then-act window

            def ocr(self, _crop, cls=True):
                return None  # no legible line — a normal outcome

        fake = ModuleType("paddleocr")
        fake.PaddleOCR = FakePaddleOCR
        monkeypatch.setitem(sys.modules, "paddleocr", fake)

        reader = PaddlePlateReader()
        vehicle = VehicleBox(0.0, 0.0, 1.0, 1.0, "car", 0.9)
        threads = [threading.Thread(target=reader.read, args=(_IMAGE, vehicle)) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
            assert not t.is_alive()

        assert FakePaddleOCR.loads == 1


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
