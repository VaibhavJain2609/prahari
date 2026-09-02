"""Plate reading — crop a vehicle box, localise the plate within it, read the
characters.

Same lazy-import discipline as `vehicles.py`: `paddleocr` is a heavyweight,
CUDA-aware dependency and must not load at module import time. `_load()` is the
only place it may appear.

`PaddlePlateReader` deliberately does NOT run its output through
`prahari_common.plates.normalise_plate` — that grammar belongs at the pipeline
boundary (see `pipeline.py`), where `raw_text` and `char_confidence` are kept
aligned for the wire per the `events.proto` invariant. This module's job stops
at "what did OCR see, and how confident was it, character by character".
"""

from __future__ import annotations

from typing import Any

import numpy as np

from prahari_inference.config import DetectorSettings, detector_settings

from .types import PlateCandidate, VehicleBox

__all__ = ["PaddlePlateReader", "ScriptedPlateReader"]


class PaddlePlateReader:
    """PaddleOCR, restricted to the cropped vehicle region.

    Cropping first rather than reading the full frame: a plate is a small
    fraction of the frame and OCR run over the whole image spends most of its
    budget on background it will discard, while raising the odds of an
    unrelated text region (a shopfront sign, a billboard) being read as a
    plate.
    """

    def __init__(self, settings: DetectorSettings | None = None) -> None:
        self._s = settings or detector_settings()
        self._ocr: Any = None

    def _load(self) -> Any:
        if self._ocr is None:
            from paddleocr import PaddleOCR

            self._ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
        return self._ocr

    def read(self, image: np.ndarray, vehicle: VehicleBox) -> PlateCandidate | None:
        crop = _crop(image, vehicle)
        if crop.size == 0:
            return None

        ocr = self._load()
        result = ocr.ocr(crop, cls=True)
        line = _best_line(result)
        if line is None:
            return None

        text, confidence = line
        if confidence < self._s.plate_confidence:
            return None

        # PaddleOCR reports one confidence for the whole line, not per
        # character. Broadcasting it is honest about what the model actually
        # measured — inventing a lower per-character number would tell the
        # matcher's confusion-aware weighting something the model never said.
        return PlateCandidate(raw_text=text, char_confidence=(confidence,) * len(text))


def _crop(image: np.ndarray, vehicle: VehicleBox) -> np.ndarray:
    height, width = image.shape[:2]
    x_min = max(0, int(vehicle.x_min * width))
    y_min = max(0, int(vehicle.y_min * height))
    x_max = min(width, int(vehicle.x_max * width))
    y_max = min(height, int(vehicle.y_max * height))
    return image[y_min:y_max, x_min:x_max]


def _best_line(result: Any) -> tuple[str, float] | None:
    """PaddleOCR's `.ocr()` returns a nested per-image, per-line structure.
    Pick the highest-confidence line: a plate crop occasionally contains a
    second line of text (a bumper sticker, a dealer frame) and the plate
    itself is almost always the more confidently read of the two."""
    if not result or not result[0]:
        return None
    best: tuple[str, float] | None = None
    for _box, (text, confidence) in result[0]:
        if best is None or confidence > best[1]:
            best = (text, confidence)
    return best


class ScriptedPlateReader:
    """A `PlateReader` whose output is authored by the test.

    Scripted by object identity of the `VehicleBox` it is asked to read, not by
    call order — `DetectionPipeline` calls `read()` once per detected vehicle,
    in an order that depends on batching, so keying by identity keeps the test
    independent of that order.
    """

    def __init__(self, script: dict[int, PlateCandidate] | None = None) -> None:
        self.script = script or {}
        self.calls: list[VehicleBox] = []

    def read(self, image: np.ndarray, vehicle: VehicleBox) -> PlateCandidate | None:
        self.calls.append(vehicle)
        return self.script.get(id(vehicle))
