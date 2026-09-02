"""The three-stage matching funnel (DAY2-DESIGN.md §7.2).

Most traffic is not on the watchlist, so this is optimised for fast, honest
rejection:

  stage 1 -- Bloom filter over skeletons, O(1), rejects ~all non-watchlist
             traffic and never produces a false negative.
  stage 2 -- bounded candidate generation: exact skeleton bucket plus
             single-deletion neighbours in both directions, O(len(plate)),
             never a full scan of the watchlist.
  stage 3 -- weighted scoring of the (small) candidate set, producing a score,
             a `ConfidenceBand`, and a full `MatchExplanation` that an officer
             can actually read.

Every alert traces back to this module's output. An unexplainable match is
worse than no match (events.proto, `MatchExplanation`'s own comment) -- so
`_score` builds the explanation from the same edits it scores, not a
paraphrase of them.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from prahari.v1 import common_pb2, events_pb2
from prahari_common.plates import normalise_plate, project_confidences

from .bloom import BloomFilter
from .config import MatchSettings
from .confusion import Edit, weighted_levenshtein
from .confusion import skeleton as fold_skeleton
from .watchlist import Watchlist, WatchlistRecord, single_char_deletions

__all__ = ["MatchResult", "WatchlistSnapshot", "WatchlistStore", "bloom_probes", "match"]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatchlistSnapshot:
    """One watchlist and the Bloom filter built from it, paired so they can
    never be observed out of sync with each other (P5)."""

    watchlist: Watchlist
    bloom: BloomFilter


class WatchlistStore:
    """Mutable holder for the live watchlist and its Bloom filter.

    Exists so `/api/v1/watchlist/reload` (app.py) is visible to the gRPC
    servicer immediately, without restarting it. A reload replaces
    `self._snapshot` with a new `WatchlistSnapshot` in one attribute
    assignment -- CPython attribute assignment is atomic under the GIL, so a
    reader can only ever see the old pair or the new pair, never one field
    from each.

    P5: the previous version stored `watchlist` and `bloom` as two separate
    attributes, replaced by two separate assignments in `replace()`. A reload
    landing between a caller's read of one and its read of the other handed
    back new-bloom + old-index -- stage 1 would pass a plate that IS on the
    reloaded watchlist, stage 2 would find no candidate against the stale
    index, and `match()` would return `no_match` for a genuine hit. Callers
    that need both fields consistent with each other MUST call `snapshot()`
    once and read both off the returned object, never `store.watchlist` and
    `store.bloom` as two separate accesses.
    """

    def __init__(self, watchlist: Watchlist, bloom: BloomFilter) -> None:
        self._snapshot = WatchlistSnapshot(watchlist, bloom)

    def snapshot(self) -> WatchlistSnapshot:
        return self._snapshot

    def replace(self, watchlist: Watchlist, bloom: BloomFilter) -> None:
        self._snapshot = WatchlistSnapshot(watchlist, bloom)

    @property
    def watchlist(self) -> Watchlist:
        """Convenience for callers that only ever need one field on its own
        (e.g. reading entry counts for a log line). Never use this together
        with `.bloom` in the same operation -- that is the exact tear P5
        fixed; take `snapshot()` once instead."""
        return self._snapshot.watchlist

    @property
    def bloom(self) -> BloomFilter:
        """See `.watchlist` -- same caveat, same reason."""
        return self._snapshot.bloom


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    band: int  # prahari.v1.ConfidenceBand
    explanation: events_pb2.MatchExplanation | None
    entry: events_pb2.WatchlistEntry | None

    @classmethod
    def no_match(cls) -> MatchResult:
        return cls(
            matched=False,
            band=common_pb2.CONFIDENCE_BAND_UNSPECIFIED,
            explanation=None,
            entry=None,
        )


def bloom_probes(observed_skeleton: str) -> Iterable[str]:
    """Every key stage 1 must check for one observed skeleton, so its
    acceptance set is a superset of `_candidates`'s (see
    `Watchlist.bloom_keys`).

    `observed_skeleton` itself covers both the exact-bucket case and the
    "watchlist entry is one character longer" case (`_by_deletion` keys are
    in the filter too). `single_char_deletions(observed_skeleton)` covers the
    remaining case -- observed is one character LONGER than the watchlist
    entry (a hallucinated glyph) -- by probing with each variant `_candidates`
    will look up under `exact_skeleton`.
    """
    yield observed_skeleton
    yield from single_char_deletions(observed_skeleton)


def _candidates(watchlist: Watchlist, observed_skeleton: str) -> list[WatchlistRecord]:
    """Bounded candidate set for one observed skeleton: the exact bucket, plus
    both directions of a single-character deletion. O(len(plate)) lookups,
    each O(1) average -- never a scan of the watchlist."""
    seen: dict[str, WatchlistRecord] = {}

    for record in watchlist.exact_skeleton(observed_skeleton):
        seen[record.entry.entry_id] = record

    # Watchlist entry is one character LONGER than what OCR read (OCR dropped
    # a character the real plate has).
    for record in watchlist.by_deletion_variant(observed_skeleton):
        seen.setdefault(record.entry.entry_id, record)

    # Observed skeleton is one character LONGER than a watchlist entry (OCR
    # hallucinated a spurious character).
    for variant in single_char_deletions(observed_skeleton):
        for record in watchlist.exact_skeleton(variant):
            seen.setdefault(record.entry.entry_id, record)

    return list(seen.values())


def _is_expired(entry: events_pb2.WatchlistEntry, now: datetime) -> bool:
    if not entry.HasField("expires_at"):
        return False
    return entry.expires_at.ToDatetime(tzinfo=UTC) <= now


def _format_plausibility(edits: tuple[Edit, ...]) -> float:
    """How much of the difference between what OCR read and this candidate is
    explained by a known confusion, restricted to actual substitutions
    (insertions/deletions carry no confusion class to be plausible about).

    No substitutions at all -- an exact match, or the only edits are
    indels -- means nothing is unexplained, so this returns 1.0 rather than
    penalising a clean match for having no confusions to point to.
    """
    substitutions = [e for e in edits if e.observed and e.matched]
    if not substitutions:
        return 1.0
    known = sum(1 for e in substitutions if e.is_known_confusion)
    return known / len(substitutions)


def _band_for(score: float, settings: MatchSettings) -> int:
    if score >= settings.confirmed_score:
        return common_pb2.CONFIDENCE_BAND_CONFIRMED
    if score >= settings.probable_score:
        return common_pb2.CONFIDENCE_BAND_PROBABLE
    if score >= settings.weak_score:
        return common_pb2.CONFIDENCE_BAND_WEAK
    return common_pb2.CONFIDENCE_BAND_UNSPECIFIED


def _score(
    observed: str,
    record: WatchlistRecord,
    confidences: tuple[float, ...],
    settings: MatchSettings,
) -> tuple[float, events_pb2.MatchExplanation]:
    import math

    weighted = weighted_levenshtein(
        observed, record.normalised.text, confidences=confidences, mask=record.normalised.mask
    )
    plausibility = _format_plausibility(weighted.edits)

    # exp() decay: a handful of cheap, plausible edits barely moves the score;
    # one full-price unrelated substitution collapses it. `plausibility`
    # further rewards a candidate whose differences are ALL known confusions
    # over one with the same distance but unexplained edits.
    decayed = math.exp(-weighted.distance / settings.score_decay)
    final_score = decayed * (0.6 + 0.4 * plausibility)

    explanation = events_pb2.MatchExplanation(
        observed_plate=observed,
        matched_plate=record.normalised.text,
        edits=[
            events_pb2.CharacterEdit(
                position=e.position,
                observed=e.observed,
                matched=e.matched,
                is_known_confusion=e.is_known_confusion,
                cost=e.cost,
            )
            for e in weighted.edits
        ],
        weighted_distance=weighted.distance,
        format_plausibility=plausibility,
        final_score=final_score,
    )
    return final_score, explanation


def match(
    plate: events_pb2.PlateReading,
    store: WatchlistStore,
    settings: MatchSettings | None = None,
    *,
    now: datetime | None = None,
) -> MatchResult:
    """Run the full funnel for one plate reading. Returns `MatchResult.no_match()`
    for anything that does not clear `settings.weak_score` -- callers
    should not treat the absence of a match as an error; most plates are not on
    the watchlist, and that is the expected case this whole design optimises
    for.

    The match engine, not inference, owns re-deriving normalisation from
    `raw_text` here -- `plate.normalised_text` is a convenience field on a
    vendor-neutral wire contract (`proto/`'s `CameraAdapter` ABI), so a
    non-Python adapter could send one that disagrees with what this codebase's
    own grammar produces, or leave it empty. Trusting it verbatim would let a
    plate that skeleton-folds differently miss the watchlist silently (M5).
    Re-deriving from `raw_text` here also gives `NormalisedPlate.source_index`
    for free, which is what lets `char_confidence` -- aligned to `raw_text` on
    the wire, per `events.proto` -- be projected onto the normalised text
    `weighted_levenshtein` actually indexes by (M4). Only when `raw_text` is
    empty (no re-derivation possible) do both fall back to the wire values.
    """
    settings = settings or MatchSettings()

    if plate.raw_text:
        normalised = normalise_plate(plate.raw_text)
        observed = normalised.text
        if plate.normalised_text and observed != plate.normalised_text:
            log.warning(
                "re-normalised plate %r disagrees with wire normalised_text %r; "
                "trusting the re-derived value",
                observed,
                plate.normalised_text,
            )
        confidences = project_confidences(list(plate.char_confidence), normalised)
    else:
        observed = plate.normalised_text
        confidences = tuple(plate.char_confidence)

    if not observed:
        return MatchResult.no_match()

    observed_skeleton = fold_skeleton(observed)

    # P5: one snapshot read, not two. `store.bloom` and `store.watchlist` as
    # separate accesses could straddle a `/api/v1/watchlist/reload` and pair
    # a fresh bloom filter with a stale index (or vice versa).
    snapshot = store.snapshot()

    # Stage 1: O(1) reject. Never a false negative -- see bloom.py. Multiple
    # probes because stage 2 accepts observations of a different length than
    # the watchlist entry (see `bloom_probes`); checking only the exact
    # skeleton here would make those paths dead code behind a false negative.
    if not any(probe in snapshot.bloom for probe in bloom_probes(observed_skeleton)):
        return MatchResult.no_match()

    # Stage 2: bounded candidate generation.
    candidates = _candidates(snapshot.watchlist, observed_skeleton)
    if not candidates:
        # The Bloom filter said "maybe"; the index says "no". This is exactly
        # what `current_false_positive_rate` predicts happening some of the
        # time -- not a bug, the one-sided error the filter trades for O(1)
        # rejection of everything else.
        return MatchResult.no_match()

    now = now or datetime.now(UTC)

    best_score = -1.0
    best_entry: events_pb2.WatchlistEntry | None = None
    best_explanation: events_pb2.MatchExplanation | None = None

    for record in candidates[: settings.max_candidates]:
        if _is_expired(record.entry, now):
            continue
        score, explanation = _score(observed, record, confidences, settings)
        if score > best_score:
            best_score, best_entry, best_explanation = score, record.entry, explanation

    if best_entry is None or best_explanation is None:
        return MatchResult.no_match()

    band = _band_for(best_score, settings)
    if band == common_pb2.CONFIDENCE_BAND_UNSPECIFIED:
        return MatchResult.no_match()

    return MatchResult(matched=True, band=band, explanation=best_explanation, entry=best_entry)
