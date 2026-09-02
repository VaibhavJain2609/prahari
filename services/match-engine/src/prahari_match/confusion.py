"""The confusion model: which characters Indian-plate OCR reliably mixes up,
and what a substitution between them should cost.

This is the reason exact-match watchlists fail silently (DAY2-DESIGN.md §7.1):
OCR returns `GJ01AB1Z34`, the watchlist holds `GJ01AB1234`, and a lookup that
does not know `2` and `Z` look alike just misses. Everything here exists to
turn that miss into a scored, explained hit.

What is deliberately NOT here: candidate generation and banding (`matcher.py`)
and the watchlist index (`watchlist.py`). This module only prices one
character against another and chains that into a weighted edit distance.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from prahari_common.plates import MASK_ALPHA, MASK_DIGIT

__all__ = [
    "Edit",
    "WeightedDistance",
    "in_same_confusion_class",
    "skeleton",
    "substitution_cost",
    "weighted_levenshtein",
]

# Bidirectional confusion classes. Membership, not direction, is what matters:
# OCR that misreads 0 as O is exactly as likely to misread O as 0.
_CONFUSION_CLASSES: tuple[frozenset[str], ...] = (
    frozenset("0ODQ"),
    frozenset("8B"),
    frozenset("1IL"),
    frozenset("5S"),
    frozenset("2Z"),
    frozenset("6G"),
)

# One canonical representative per class, chosen arbitrarily but fixed, so
# `skeleton()` is deterministic. Digits, since a skeleton is compared against
# other skeletons rather than displayed.
_CANONICAL_BY_CLASS: dict[int, str] = {0: "0", 1: "8", 2: "1", 3: "5", 4: "2", 5: "6"}

_CLASS_OF: dict[str, int] = {ch: idx for idx, cls in enumerate(_CONFUSION_CLASSES) for ch in cls}

# Substitution cost, before confidence and position are applied. Cheap inside a
# confusion class; full price outside it, which is what makes an unrelated
# swap (0 -> W) survive the funnel far less often than a confusable one.
_CONFUSION_BASE_COST = 0.4
_UNRELATED_BASE_COST = 3.0

# A substitution at a character OCR was unsure of is cheap regardless of class
# -- low confidence IS the model telling us it might have misread this glyph.
# The floor keeps even a zero-confidence character from going to zero cost,
# which would let a single garbage character absorb an arbitrary substitution
# for free.
_CONFIDENCE_FLOOR = 0.25

# A digit read where the format mask demands a letter (or vice versa) is
# near-certain OCR confusion: 0/O, 1/I, 5/S, 2/Z look alike specifically
# *because* one is a digit and the other a letter. Applied only on top of an
# already-cheap confusion-class cost, per DAY2-DESIGN §7.2 (3).
_POSITIONAL_DISCOUNT = 0.3

# Cost of inserting or deleting a character (a dropped or spurious glyph).
# Between the two substitution tiers: a real failure mode, but a distinct one
# from a confusion, so it does not get the confusion-class discount.
_INDEL_COST = 1.2


def in_same_confusion_class(a: str, b: str) -> bool:
    """Whether `a` and `b` are two glyphs OCR is known to mix up. `a == b` is
    not a confusion -- it is not a substitution at all."""
    if a == b:
        return False
    class_a = _CLASS_OF.get(a)
    class_b = _CLASS_OF.get(b)
    return class_a is not None and class_a == class_b


def skeleton(text: str) -> str:
    """Fold every confusable character onto its class's canonical
    representative, so `GJ01AB1Z34` and `GJ01AB1234` produce the same string.

    This is stage 1 of the matcher: a Bloom filter and an index are built over
    skeletons, not raw plate text, which is what lets an OCR misread land in
    the same bucket as the plate it actually is.
    """
    return "".join(_CANONICAL_BY_CLASS.get(_CLASS_OF.get(ch, -1), ch) for ch in text)


def _char_kind(ch: str) -> str:
    if ch.isdigit():
        return "digit"
    if ch.isalpha():
        return "alpha"
    return "other"


def substitution_cost(
    observed: str,
    candidate: str,
    *,
    confidence: float = 0.0,
    mask_char: str | None = None,
) -> tuple[float, bool]:
    """Cost of reading `observed` where the watchlist has `candidate` at this
    position, and whether the pair is a known confusion.

    Three inputs, per DAY2-DESIGN §7.2 (3):

    1. Confusion-class membership -- cheap inside a class, full price outside.
    2. `confidence`, OCR's own certainty about the observed character. Low
       confidence makes ANY substitution here cheaper, because the model is
       telling us it may have guessed.
    3. `mask_char`, the candidate's positional format class (`MASK_ALPHA` /
       `MASK_DIGIT` / `MASK_FREE` / `None`). A same-class swap that also
       crosses the alpha/digit boundary the mask expects (a letter read where
       a digit belongs, or vice versa) is near-certain OCR confusion and gets
       a further discount on top of (1).
    """
    if observed == candidate:
        return 0.0, False

    same_class = in_same_confusion_class(observed, candidate)
    base = _CONFUSION_BASE_COST if same_class else _UNRELATED_BASE_COST

    confidence = min(max(confidence, 0.0), 1.0)
    confidence_factor = _CONFIDENCE_FLOOR + (1.0 - _CONFIDENCE_FLOOR) * confidence
    cost = base * confidence_factor

    if same_class and mask_char in (MASK_ALPHA, MASK_DIGIT):
        expected = "alpha" if mask_char == MASK_ALPHA else "digit"
        observed_kind = _char_kind(observed)
        candidate_kind = _char_kind(candidate)
        if candidate_kind == expected and observed_kind != expected:
            cost *= _POSITIONAL_DISCOUNT

    return cost, same_class


@dataclass(frozen=True)
class Edit:
    """One edit applied to turn `observed` into `matched`. Mirrors
    `prahari.v1.CharacterEdit` field-for-field so `matcher.py` can build the
    proto message with no translation logic to get wrong.

    `position` indexes the MATCHED (watchlist) text, not the observed one --
    the watchlist plate is the fixed reference an officer reads, while the
    observed string may be a different length after an insertion or deletion.

    A pure deletion (observed had an extra character) carries `matched=""`; a
    pure insertion (observed is missing a character) carries `observed=""`.
    """

    position: int
    observed: str
    matched: str
    is_known_confusion: bool
    cost: float


@dataclass(frozen=True)
class WeightedDistance:
    distance: float
    edits: tuple[Edit, ...]


def _confidence_at(confidences: Sequence[float], index: int) -> float:
    """Confidence for one raw-text position. Missing or short input defaults to
    0.0 -- "trust this character least" -- mirroring
    `prahari_common.plates.project_confidences`'s own default, so a backend
    that does not report per-character confidence degrades to "every
    substitution is cheap" rather than "every substitution is full price"."""
    if 0 <= index < len(confidences):
        return confidences[index]
    return 0.0


def weighted_levenshtein(
    observed: str,
    candidate: str,
    *,
    confidences: Sequence[float] = (),
    mask: str = "",
) -> WeightedDistance:
    """Weighted edit distance from `observed` (what OCR read) to `candidate`
    (a watchlist plate), plus the list of edits that produced it.

    `confidences` is index-aligned with `observed`. `mask` is index-aligned
    with `candidate` (its positional format template from
    `prahari_common.plates`). Both may be shorter than their string or absent
    entirely; missing entries fall back to "no signal" rather than raising, so
    a caller that has no mask (e.g. a NONCONFORMING candidate) still gets a
    plain confusion-aware distance.
    """
    n, m = len(observed), len(candidate)

    # dp[i][j] = cost of turning observed[:i] into candidate[:j].
    # op[i][j]  records which transition achieved it, so the edit list is read
    # off directly rather than re-deriving it from float equality checks.
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    op: list[list[str]] = [["match"] * (m + 1) for _ in range(n + 1)]

    for i in range(1, n + 1):
        dp[i][0] = i * _INDEL_COST
        op[i][0] = "del"
    for j in range(1, m + 1):
        dp[0][j] = j * _INDEL_COST
        op[0][j] = "ins"

    for i in range(1, n + 1):
        obs_char = observed[i - 1]
        confidence = _confidence_at(confidences, i - 1)
        for j in range(1, m + 1):
            cand_char = candidate[j - 1]
            mask_char = mask[j - 1] if j - 1 < len(mask) else None

            if obs_char == cand_char:
                sub_cost, known = 0.0, False
            else:
                sub_cost, known = substitution_cost(
                    obs_char, cand_char, confidence=confidence, mask_char=mask_char
                )

            best_cost = dp[i - 1][j - 1] + sub_cost
            best_op = "match" if sub_cost == 0.0 else "sub"

            del_cost = dp[i - 1][j] + _INDEL_COST
            if del_cost < best_cost:
                best_cost, best_op = del_cost, "del"

            ins_cost = dp[i][j - 1] + _INDEL_COST
            if ins_cost < best_cost:
                best_cost, best_op = ins_cost, "ins"

            dp[i][j] = best_cost
            op[i][j] = best_op
            # `known` only matters for "sub"; stash it via a side table keyed
            # the same way, keeping `op` itself a plain string grid.
            if best_op == "sub":
                op[i][j] = "sub-known" if known else "sub-unknown"

    edits: list[Edit] = []
    i, j = n, m
    while i > 0 or j > 0:
        step = op[i][j]
        if step == "match":
            i, j = i - 1, j - 1
        elif step in ("sub-known", "sub-unknown"):
            cost = dp[i][j] - dp[i - 1][j - 1]
            edits.append(
                Edit(
                    position=j - 1,
                    observed=observed[i - 1],
                    matched=candidate[j - 1],
                    is_known_confusion=step == "sub-known",
                    cost=cost,
                )
            )
            i, j = i - 1, j - 1
        elif step == "del":
            cost = dp[i][j] - dp[i - 1][j]
            edits.append(
                Edit(
                    position=j,
                    observed=observed[i - 1],
                    matched="",
                    is_known_confusion=False,
                    cost=cost,
                )
            )
            i -= 1
        else:  # "ins"
            cost = dp[i][j] - dp[i][j - 1]
            edits.append(
                Edit(
                    position=j - 1,
                    observed="",
                    matched=candidate[j - 1],
                    is_known_confusion=False,
                    cost=cost,
                )
            )
            j -= 1

    edits.reverse()
    return WeightedDistance(distance=dp[n][m], edits=tuple(edits))
