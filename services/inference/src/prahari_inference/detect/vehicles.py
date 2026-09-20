"""Vehicle detection — the first expensive stage, and the only one gated by
`MotionGate` upstream.

Two backends behind one `VehicleDetector` protocol (types.py): `YoloVehicleDetector`
for real inference and `ScriptedVehicleDetector` for tests. `ultralytics` is a
multi-gigabyte install with a CUDA-aware dependency tree; importing it at module
load time would mean `make test` needs it on disk, which is exactly the failure
`types.py`'s docstring warns about. The import happens inside `_load()`, on first
`detect()` call, never at import time.
"""

from __future__ import annotations

import threading
from typing import Any

from prahari_inference.config import DetectorSettings, detector_settings

from .types import SampledFrame, VehicleBox

__all__ = ["ScriptedVehicleDetector", "YoloVehicleDetector"]

# COCO class ids this cascade cares about. Everything else YOLO finds
# (person, traffic light, ...) is discarded here rather than downstream, so a
# vehicle box is a contract every later stage can rely on without re-filtering.
_VEHICLE_CLASSES: dict[int, str] = {
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
}


class YoloVehicleDetector:
    """Ultralytics YOLO, restricted to vehicle classes.

    `DetectorSettings.model` names the weights file (`yolov8n.pt` on the laptop,
    a larger variant under the gpu profile) — a value the chart sets, never a
    branch this class takes on its own. `decode_backend` and `device` similarly
    stay passed-through settings; probing for CUDA here (or letting ultralytics
    auto-select it by omitting `device=`) would reintroduce exactly the runtime
    sniffing the `profile` invariant exists to forbid.
    """

    def __init__(self, settings: DetectorSettings | None = None) -> None:
        self._s = settings or detector_settings()
        self._model: Any = None
        # `detect()` runs on every pump thread that fills a batch AND on the
        # batcher's flush thread, so two threads can reach `_load()` together
        # on a cold start. Without the lock both see `_model is None` and both
        # pay the multi-second weights load — the loser's model is silently
        # discarded, the winner's predict() possibly torn mid-call.
        self._load_lock = threading.Lock()

    def _load(self) -> Any:
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    from ultralytics import YOLO

                    self._model = YOLO(self._s.model)
        return self._model

    def detect(self, frames: list[SampledFrame]) -> list[list[VehicleBox]]:
        if not frames:
            return []
        model = self._load()
        images = [f.image for f in frames]
        # `classes=` filters inside the model rather than after, so confidence
        # thresholding and NMS never spend time on boxes this cascade discards.
        # `device=` is passed explicitly: without it ultralytics silently
        # auto-selects CUDA when it is present, which is backend sniffing —
        # the device comes from `DetectorSettings.device`, set by the profile.
        results = model.predict(
            images,
            conf=self._s.vehicle_confidence,
            classes=list(_VEHICLE_CLASSES),
            device=self._s.device,
            verbose=False,
        )
        return [
            self._to_boxes(frame, result) for frame, result in zip(frames, results, strict=True)
        ]

    @staticmethod
    def _to_boxes(frame: SampledFrame, result: Any) -> list[VehicleBox]:
        height, width = frame.image.shape[:2]
        boxes: list[VehicleBox] = []
        for box in result.boxes:
            x_min, y_min, x_max, y_max = (float(v) for v in box.xyxy[0])
            class_id = int(box.cls[0])
            boxes.append(
                VehicleBox(
                    x_min=x_min / width,
                    y_min=y_min / height,
                    x_max=x_max / width,
                    y_max=y_max / height,
                    vehicle_class=_VEHICLE_CLASSES.get(class_id, "vehicle"),
                    confidence=float(box.conf[0]),
                )
            )
        return boxes


class ScriptedVehicleDetector:
    """A `VehicleDetector` whose output is authored by the test, not a model.

    Scripted by camera id rather than by call order: the batcher interleaves
    frames from every active camera into one `detect()` call in an order the
    test does not control, so keying the script by call position would make
    tests fragile against batching changes that have nothing to do with
    detection logic.
    """

    def __init__(self, script: dict[str, list[VehicleBox]] | None = None) -> None:
        self.script = script or {}
        self.calls: list[list[SampledFrame]] = []

    def detect(self, frames: list[SampledFrame]) -> list[list[VehicleBox]]:
        self.calls.append(frames)
        return [self.script.get(frame.camera_id, []) for frame in frames]
