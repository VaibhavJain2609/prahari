"""The detection cascade: decoded frame → vehicle → plate crop → text → event.

Cheap stages gate expensive ones. A sampled frame meets the motion gate before
a single tensor is allocated, and on typical CCTV most frames stop there — that
skip ratio is the streams-per-GPU number, not a micro-optimisation.

Stage contracts live in `types`; see that module for why every backend has a
scripted fake.
"""

from .batching import CrossCameraBatcher
from .motion import MotionGate
from .pipeline import DetectionPipeline
from .plates import PaddlePlateReader, ScriptedPlateReader
from .tamper import TamperDetector
from .types import (
    DetectionResult,
    PlateCandidate,
    PlateReader,
    SampledFrame,
    TamperReport,
    VehicleBox,
    VehicleDetector,
)
from .vehicles import ScriptedVehicleDetector, YoloVehicleDetector

__all__ = [
    "CrossCameraBatcher",
    "DetectionPipeline",
    "DetectionResult",
    "MotionGate",
    "PaddlePlateReader",
    "PlateCandidate",
    "PlateReader",
    "SampledFrame",
    "ScriptedPlateReader",
    "ScriptedVehicleDetector",
    "TamperDetector",
    "TamperReport",
    "VehicleBox",
    "VehicleDetector",
    "YoloVehicleDetector",
]
