"""The seams of the detection cascade.

Every stage boundary is declared here, once, so that the stages can be written,
tested and replaced independently. Two properties matter more than elegance:

**Backends are Protocols, and each has a scripted fake.** `ultralytics` and
`paddleocr` pull in multi-gigabyte wheels. If `make test` needs them, the suite
stops being run, and a suite that is not run is how a broken service reaches
demo day looking green. Every boundary below must therefore be exercisable with
no model file on disk.

**Nothing here corrects a plate.** `PlateCandidate` carries what OCR saw plus
per-character confidence, and that is all. Correction is the match engine's job,
where it is scored and auditable — `events.proto` states this as an invariant
and this module is where it would be easiest to violate by accident.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np

from prahari_inference.timing import FrameTiming

__all__ = [
    "DetectionResult",
    "PlateCandidate",
    "PlateReader",
    "SampledFrame",
    "TamperReport",
    "VehicleBox",
    "VehicleDetector",
]


@dataclass(frozen=True)
class SampledFrame:
    """One frame that survived sampling and is a candidate for detection.

    Carries its camera id because the batcher mixes cameras: a batch is
    assembled ACROSS cameras, so a frame that does not know where it came from
    cannot be attributed after inference returns.
    """

    camera_id: str
    image: np.ndarray
    timing: FrameTiming


@dataclass(frozen=True)
class VehicleBox:
    """One detected vehicle, in normalised frame coordinates.

    Normalised [0.0, 1.0] rather than pixels so the box survives a resolution
    change mid-estate — the feeds are explicitly mixed-resolution — and so it
    maps directly onto `prahari.v1.BoundingBox` with no conversion step to get
    wrong.
    """

    x_min: float
    y_min: float
    x_max: float
    y_max: float
    vehicle_class: str
    confidence: float


@dataclass(frozen=True)
class PlateCandidate:
    """What OCR saw on one plate. Deliberately uncorrected.

    `char_confidence` is aligned index-wise with `raw_text` and MUST reach the
    match engine intact. The confusion-aware matcher weights each substitution
    by the confidence of the character it is replacing; collapsing this to a
    single scalar is the most common way to accidentally cripple match accuracy,
    and it fails silently — the matcher still returns answers, just worse ones.
    """

    raw_text: str
    char_confidence: tuple[float, ...]
    box: VehicleBox | None = None
    vehicle_index: int | None = None
    """Index into the batch's per-frame vehicle list this candidate was read
    against. Never set by a `PlateReader` backend -- a reader has no view of
    the vehicle list's ordering, only the single crop it was handed. Stamped
    on afterwards by `DetectionPipeline.process_batch`, which calls `read()`
    once per vehicle and therefore knows the pairing exactly; `pipeline.py`'s
    `_pair_plates_to_vehicles` trusts this field over re-deriving the pairing
    from position, which is unsound the moment any vehicle in the batch has no
    legible plate."""

    def __post_init__(self) -> None:
        if len(self.char_confidence) not in (0, len(self.raw_text)):
            raise ValueError(
                "char_confidence must be empty or one entry per character of raw_text; "
                f"got {len(self.char_confidence)} for {len(self.raw_text)} characters"
            )


@dataclass(frozen=True)
class TamperReport:
    """Per-frame tamper observation.

    Reported, never adjudicated. Same rule as camera health: a worker states
    what it measured and the registry decides what that means, because two
    workers on one camera must not be able to publish contradictory verdicts.
    """

    black_frame_ratio: float
    """Fraction of the recent window whose mean luma was below threshold."""

    blur_variance: float
    """Laplacian variance. Low means defocused or physically covered."""

    scene_changed: bool
    """A histogram break against the recent frame — AND not explained by a loop
    boundary. The feeds are recordings that loop; the wrap is an abrupt scene
    cut on every camera, every cycle, so a detector that does not whitelist it
    fires continuously and gets switched off, which is the same as absent."""

    suspected: bool
    """The detector's summary. Still an observation, not a verdict."""

    reason: str | None = None


@dataclass(frozen=True)
class DetectionResult:
    """Everything one sampled frame produced."""

    camera_id: str
    timing: FrameTiming
    vehicles: list[VehicleBox] = field(default_factory=list)
    plates: list[PlateCandidate] = field(default_factory=list)
    tamper: TamperReport | None = None
    motion_skipped: bool = False
    """True when the motion gate dropped the frame before the detector ran.
    Counted, not discarded: the skip ratio is the cost lever the whole
    streams-per-GPU argument rests on, so it has to be measurable."""


@runtime_checkable
class VehicleDetector(Protocol):
    """Frame batch in, per-frame vehicle boxes out.

    Batched because batching across cameras is worth several times per-frame
    inference, and because a per-frame interface makes that impossible to add
    later without changing every caller.
    """

    def detect(self, frames: list[SampledFrame]) -> list[list[VehicleBox]]:
        """Return one list of boxes per input frame, in input order."""
        ...


@runtime_checkable
class PlateReader(Protocol):
    """Vehicle crop in, plate reading out."""

    def read(self, image: np.ndarray, vehicle: VehicleBox) -> PlateCandidate | None:
        """Localise and read a plate within `vehicle`'s region of `image`.

        Returns None when no plate is legible. That is a normal outcome, not an
        error: an illegible plate still leaves a vehicle detection behind, and
        the appearance embedding is what bridges the gap on Day 3.
        """
        ...
