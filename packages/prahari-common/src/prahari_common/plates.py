"""Indian registration-plate grammar. The shared vocabulary for plate text.

This lives in the shared package for one reason: **the inference service and the
match engine must agree, exactly, on what a plate looks like.** If inference
normalises `GJ 01 AB 1234` one way and the watchlist loader normalises it
another, every lookup misses and nothing raises. That is the silent failure the
entire match-engine design exists to prevent, so the vocabulary cannot live in
either consumer.

What is here: grammar — stripping, uppercasing, format classification, and the
positional character mask that falls out of a format.

What is deliberately NOT here: tolerance. Confusion classes, edit costs and
scoring belong to the match engine, where a correction is scored and auditable.

INVARIANT (also stated in `events.proto`): normalisation never *corrects* a
plate. It removes separators and case. It does not turn `O` into `0` because a
position wants a digit — that is a match-time decision with a score attached,
not a parse-time one. A silently corrected plate is unexplainable in court.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "MASK_ALPHA",
    "MASK_DIGIT",
    "MASK_FREE",
    "NormalisedPlate",
    "PlateFormat",
    "normalise_plate",
    "project_confidences",
]


class PlateFormat(IntEnum):
    """Mirrors `prahari.v1.PlateFormat`. Values must stay identical — this is
    written straight onto the wire field, so a drifting number is a silent
    mislabel rather than an error."""

    UNSPECIFIED = 0
    STANDARD = 1
    BH_SERIES = 2
    MILITARY = 3
    NONCONFORMING = 4


MASK_ALPHA = "A"
"""Mask character for a position the format requires to be a letter."""

MASK_DIGIT = "9"
"""Mask character for a position the format requires to be a digit."""

MASK_FREE = "?"
"""Mask character for a position under no constraint — every position of a
NONCONFORMING plate, since by definition we do not know its shape."""

# Separators, and the `IND` country prefix stamped on newer high-security
# plates. Stripped because they carry no identity: the same vehicle is written
# `GJ01AB1234`, `GJ-01-AB-1234` and `IND GJ 01 AB 1234` by three different
# sources, and all three must land on one key.
_STRIP = re.compile(r"[^A-Z0-9]")
_IND_PREFIX = re.compile(r"^IND")

# Standard civilian: state (2 alpha) · RTO district (1-2 digits) · series
# (0-3 alpha) · number (4 digits). The series is genuinely variable-length and
# genuinely optional — early Gujarat registrations have none — so anchoring it
# at two characters rejects real plates.
_STANDARD = re.compile(r"^([A-Z]{2})(\d{1,2})([A-Z]{0,3})(\d{4})$")

# Bharat series: `YY BH NNNN LL`. Matched before the standard pattern would
# never fire anyway (it starts with digits), but ordered first for clarity.
_BH_SERIES = re.compile(r"^(\d{2})(BH)(\d{4})([A-Z]{1,2})$")

# Military: `NN L NNNNNN L`. The leading broad-arrow glyph is not representable
# in OCR output and is not expected here.
_MILITARY = re.compile(r"^(\d{2})([A-Z])(\d{6})([A-Z])$")


@dataclass(frozen=True)
class NormalisedPlate:
    """The result of parsing one plate reading."""

    text: str
    """Uppercased, separators removed. Never character-corrected."""

    format: PlateFormat

    mask: str
    """Positional character classes, same length as `text`. `AA99AA9999` for a
    standard plate. The match engine uses this to price a substitution: a digit
    read where the format demands a letter is a near-certain OCR confusion,
    which is far stronger evidence than generic edit distance."""

    source_index: tuple[int, ...]
    """For each character of `text`, its index in the raw input.

    Not bookkeeping. `PlateReading.char_confidence` is aligned to the *raw*
    string, and stripping separators shifts every position after the first one.
    Re-aligning by hand at each call site is how per-character confidence
    quietly desynchronises from the characters it describes — after which the
    matcher is weighting the wrong glyphs and still looks like it works."""

    @property
    def is_parsed(self) -> bool:
        return self.format not in (PlateFormat.UNSPECIFIED, PlateFormat.NONCONFORMING)


def _mask_for(text: str) -> tuple[PlateFormat, str]:
    """Classify `text` and return its positional mask.

    Order matters only for readability here; the three patterns are mutually
    exclusive on their first character class.
    """
    if m := _BH_SERIES.match(text):
        year, bh, number, series = m.groups()
        return PlateFormat.BH_SERIES, (
            MASK_DIGIT * len(year)
            + MASK_ALPHA * len(bh)
            + MASK_DIGIT * len(number)
            + MASK_ALPHA * len(series)
        )
    if m := _MILITARY.match(text):
        prefix, code, number, check = m.groups()
        return PlateFormat.MILITARY, (
            MASK_DIGIT * len(prefix)
            + MASK_ALPHA * len(code)
            + MASK_DIGIT * len(number)
            + MASK_ALPHA * len(check)
        )
    if m := _STANDARD.match(text):
        state, district, series, number = m.groups()
        return PlateFormat.STANDARD, (
            MASK_ALPHA * len(state)
            + MASK_DIGIT * len(district)
            + MASK_ALPHA * len(series)
            + MASK_DIGIT * len(number)
        )
    # Legible but unparseable. Kept, not discarded: a plate we cannot classify
    # is still evidence that a vehicle passed a camera, and dropping it makes
    # route reconstruction lie by omission rather than by error.
    return PlateFormat.NONCONFORMING, MASK_FREE * len(text)


def normalise_plate(raw: str) -> NormalisedPlate:
    """Parse one plate reading. Never raises; unparseable input is a format,
    not an exception."""
    upper = raw.upper()

    kept: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(upper):
        if not _STRIP.match(ch):
            kept.append(ch)
            index.append(i)

    text = "".join(kept)

    # The `IND` prefix is stripped only once the string is otherwise clean,
    # because in the raw reading it may be separated by anything at all.
    #
    # Guarded on length: a genuine plate `INDORE`-style state code does not
    # exist, but a three-character read that IS "IND" would otherwise normalise
    # to the empty string and match everything in stage 2 of the funnel.
    if len(text) > 3 and _IND_PREFIX.match(text):
        text = text[3:]
        index = index[3:]

    fmt, mask = _mask_for(text)
    return NormalisedPlate(text=text, format=fmt, mask=mask, source_index=tuple(index))


def project_confidences(
    raw_confidences: list[float] | tuple[float, ...],
    plate: NormalisedPlate,
    *,
    default: float = 0.0,
) -> tuple[float, ...]:
    """Re-align per-character confidences onto the normalised text.

    `raw_confidences` is indexed against the string that was passed to
    `normalise_plate`. The returned tuple is indexed against `plate.text`, one
    entry per surviving character.

    A short or absent confidence list yields `default` rather than raising:
    an OCR backend that does not report per-character confidence must still be
    usable, and the matcher already treats a low confidence as "cheap to
    substitute" — so the conservative default is 0.0, meaning "trust this
    character least", not 1.0.
    """
    out: list[float] = []
    for src in plate.source_index:
        out.append(raw_confidences[src] if src < len(raw_confidences) else default)
    return tuple(out)
