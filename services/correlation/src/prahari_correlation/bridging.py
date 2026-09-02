"""Appearance-embedding gap bridging (DAY3-DESIGN.md §3.3): when the plate is
unreadable on a detection but `appearance_embedding` is present, decide
whether it plausibly continues the vehicle seen either side of it.

This is a cosine-similarity threshold over whatever low-dimensional embedding
the inference cascade already emits -- `events.proto`'s own comment is
explicit that this rides the metadata plane, so no re-ID model is trained or
shipped here. A bridged hop is a materially weaker claim than a plate match
(a fuzzy plate match is at least reading the same identifier; a bridge is
reading "these two crops probably look like the same vehicle"), so it is
never fused into a `plate` link kind -- see `routes.py`, which is the only
place `LinkKind.BRIDGED` is assigned.
"""

from __future__ import annotations

from math import sqrt

__all__ = ["cosine_similarity", "is_bridge_candidate"]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sqrt(sum(x * x for x in a))
    norm_b = sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def is_bridge_candidate(
    left_embedding: list[float],
    unreadable_embedding: list[float],
    right_embedding: list[float],
    threshold: float,
) -> bool:
    """Both neighbouring similarities must clear `threshold` -- matching only
    one side is exactly the false-positive shape this exists to avoid: a
    vehicle that merely looks similar to the one before it, coincidentally
    also looking similar to something after it, is not evidence of a single
    continuous journey."""
    return (
        cosine_similarity(left_embedding, unreadable_embedding) >= threshold
        and cosine_similarity(unreadable_embedding, right_embedding) >= threshold
    )
