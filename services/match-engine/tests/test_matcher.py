"""matcher.py: the three-stage funnel end to end.

Every number asserted here was produced by actually running the matcher (see
the accuracy table below), not derived on paper -- the scoring formula
combines three inputs (confusion class, confidence, format mask) and hand
arithmetic is exactly the kind of thing that looks right and is not.
"""

from __future__ import annotations

import pytest
from prahari.v1 import common_pb2, events_pb2

from prahari_match.bloom import BloomFilter
from prahari_match.config import MatchSettings
from prahari_match.matcher import WatchlistStore, match
from prahari_match.watchlist import Watchlist


def _store_with(*plates: str, reason: int = events_pb2.WATCHLIST_REASON_STOLEN) -> WatchlistStore:
    watchlist = Watchlist()
    for i, plate in enumerate(plates):
        watchlist.add(events_pb2.WatchlistEntry(entry_id=f"E{i}", plate=plate, reason=reason))
    # Mirrors production `app._build_bloom`: seeded from `bloom_keys()` (exact
    # skeletons plus deletion variants), not `skeletons()` alone -- otherwise
    # these tests would pass on a filter stage 2's own candidate generation
    # can outrun, which is exactly the M1 false negative.
    keys = list(watchlist.bloom_keys())
    bloom = BloomFilter(expected_items=max(100, len(keys)))
    for key in keys:
        bloom.add(key)
    return WatchlistStore(watchlist, bloom)


def _reading(
    normalised_text: str, confidences: list[float] | None = None
) -> events_pb2.PlateReading:
    confidences = confidences if confidences is not None else [0.9] * len(normalised_text)
    return events_pb2.PlateReading(normalised_text=normalised_text, char_confidence=confidences)


SETTINGS = MatchSettings()


# --- accuracy table -----------------------------------------------------------
# One row per confusion class DAY2-DESIGN.md §7.2 names, in the direction that
# actually occurs on these plates: a digit position OCR misread as its
# letter-shaped twin. Every row confirmed by running the matcher (see the
# module docstring), not computed by hand.
ACCURACY_TABLE = [
    # (watchlist plate, OCR misread, confusion class under test)
    ("GJ01AB1230", "GJ01AB123O", "0/O/D/Q"),
    ("GJ01AB1238", "GJ01AB123B", "8/B"),
    ("GJ01AB1231", "GJ01AB123I", "1/I/L (I)"),
    ("GJ01AB1231", "GJ01AB123L", "1/I/L (L)"),
    ("GJ01AB1235", "GJ01AB123S", "5/S"),
    ("GJ01AB1232", "GJ01AB123Z", "2/Z"),
    ("GJ01AB1236", "GJ01AB123G", "6/G"),
]


@pytest.mark.parametrize("watchlist_plate,ocr_misread,confusion_class", ACCURACY_TABLE)
def test_accuracy_table_confirms_every_confusion_class(
    watchlist_plate: str, ocr_misread: str, confusion_class: str
) -> None:
    store = _store_with(watchlist_plate)
    result = match(_reading(ocr_misread), store, SETTINGS)

    assert result.matched, f"{confusion_class}: {ocr_misread!r} should hit {watchlist_plate!r}"
    assert result.band == common_pb2.CONFIDENCE_BAND_CONFIRMED, confusion_class
    assert result.entry.plate == watchlist_plate
    assert result.explanation.final_score == pytest.approx(0.9637, abs=0.002)
    # Exactly the substituted character should be flagged as a known
    # confusion -- not every character, and not zero of them.
    known_confusions = [e for e in result.explanation.edits if e.is_known_confusion]
    assert len(known_confusions) == 1
    assert known_confusions[0].observed == ocr_misread[-1]
    assert known_confusions[0].matched == watchlist_plate[-1]


def test_confusion_is_bidirectional_in_practice() -> None:
    # The letter->digit direction (a NONCONFORMING watchlist plate, since a
    # letter where the format wants a digit does not parse) still matches,
    # just without the positional-mask discount the digit->letter direction
    # gets -- so the score is lower but still comfortably CONFIRMED.
    store = _store_with("GJ01AB123O")
    result = match(_reading("GJ01AB1230"), store, SETTINGS)
    assert result.matched
    assert result.band == common_pb2.CONFIDENCE_BAND_CONFIRMED


# --- negative-direction: plausible plates that must NOT match ----------------


def test_unrelated_single_character_difference_does_not_match() -> None:
    # GJ01AB1235 is one digit off GJ01AB1234 -- but 4 and 5 are not a known
    # confusion, so this is two DIFFERENT real plates, not an OCR misread of
    # one plate. If this matched, every vehicle sharing 9 of 10 characters
    # with a watchlist plate would falsely alert.
    store = _store_with("GJ01AB1234")
    result = match(_reading("GJ01AB1235", confidences=[1.0] * 10), store, SETTINGS)
    assert not result.matched


def test_unrelated_difference_does_not_match_even_at_low_confidence() -> None:
    # Low confidence makes a KNOWN confusion cheaper; it must not manufacture
    # a match out of a substitution that was never a confusion to begin with.
    # Candidate generation (stage 2) never even considers this pair, because
    # its skeleton is not the watchlist entry's skeleton and not a
    # single-deletion neighbour of it -- confidence has no path to matter here.
    store = _store_with("GJ01AB1234")
    result = match(_reading("GJ01AB1235", confidences=[0.0] * 10), store, SETTINGS)
    assert not result.matched


def test_completely_different_plate_is_rejected_at_stage_one() -> None:
    store = _store_with("GJ01AB1234")
    result = match(_reading("MH12ZZ9999"), store, SETTINGS)
    assert not result.matched
    assert result.band == common_pb2.CONFIDENCE_BAND_UNSPECIFIED


def test_empty_observed_text_does_not_match() -> None:
    store = _store_with("GJ01AB1234")
    result = match(_reading(""), store, SETTINGS)
    assert not result.matched


# --- M1: the bloom funnel must not false-negative on a length mismatch ------
# `test_bloom.py` proves the BloomFilter data structure itself never false-
# negatives. These prove the FUNNEL doesn't -- stage 1 must accept every
# observation stage 2 can still turn into a candidate, even though its
# skeleton differs in length from the watchlist entry's.


def test_funnel_survives_a_dropped_character() -> None:
    # Routine plate-crop failure: OCR dropped the "2", so the observed
    # skeleton is one character SHORTER than the watchlist entry's. Before
    # M1, stage 1 was built from full-length skeletons only and rejected this
    # before stage 2's `by_deletion_variant` ever ran.
    store = _store_with("GJ01AB1234")
    result = match(_reading("GJ01AB134", confidences=[0.9] * 9), store, SETTINGS)
    assert result.matched
    assert result.entry.plate == "GJ01AB1234"


def test_funnel_survives_a_hallucinated_character() -> None:
    # OCR hallucinated an extra "3", so the observed skeleton is one
    # character LONGER than the watchlist entry's. Stage 2's
    # `single_char_deletions(observed)` -> `exact_skeleton` path needs stage 1
    # to accept the deletion variant, not just the observed skeleton itself.
    store = _store_with("GJ01AB1234")
    result = match(_reading("GJ01AB12334", confidences=[0.9] * 11), store, SETTINGS)
    assert result.matched
    assert result.entry.plate == "GJ01AB1234"


# --- M4/M5: observed side is re-derived from raw_text, not trusted from the
# wire, and char_confidence is projected onto normalised positions -----------


def test_char_confidence_is_projected_from_raw_text_to_normalised_positions() -> None:
    # A backend that reports `raw_text` with separators -- as any real OCR
    # backend will, see plates.py's HSRP example -- must have its confidences
    # re-indexed onto the normalised text before scoring, not read at the
    # normalised position of the raw array. Confirmed by cross-checking
    # against an equivalent clean reading with confidences already supplied
    # in normalised-text order.
    store = _store_with("GJ01AB1230")

    clean = match(_reading("GJ01AB123O", confidences=[0.99] * 9 + [0.5]), store, SETTINGS)

    raw_text = "GJ 01 AB 123O"  # normalises to GJ01AB123O; source_index skips the two spaces
    raw_confidences = [0.99] * len(raw_text)
    raw_confidences[raw_text.index("O")] = 0.5  # confidence of the actually misread char
    reading = events_pb2.PlateReading(raw_text=raw_text, char_confidence=raw_confidences)
    projected = match(reading, store, SETTINGS)

    assert projected.matched and clean.matched
    assert projected.explanation.final_score == pytest.approx(clean.explanation.final_score)


def test_wire_normalised_text_is_not_trusted_over_a_re_derivation_from_raw_text() -> None:
    # `normalised_text` is a convenience field on a vendor-neutral wire
    # contract (the `CameraAdapter` plugin ABI) -- a non-Python adapter could
    # send one that disagrees with what this codebase's own grammar produces,
    # or leave separators in. The match must go by `raw_text`, re-normalised
    # here, never by trusting the wire value verbatim.
    store = _store_with("GJ01AB1234")
    reading = events_pb2.PlateReading(
        raw_text="GJ01AB1234",
        normalised_text="GJ-01-AB-1234",  # disagrees with normalise_plate(raw_text)
        char_confidence=[0.9] * 10,
    )
    result = match(reading, store, SETTINGS)
    assert result.matched
    assert result.entry.plate == "GJ01AB1234"


def test_falls_back_to_wire_normalised_text_when_raw_text_is_absent() -> None:
    # An adapter that never sends raw_text has nothing to re-derive from --
    # the only remaining option is to trust the wire's normalised_text, with
    # raw (unprojected) confidences.
    store = _store_with("GJ01AB1234")
    reading = events_pb2.PlateReading(normalised_text="GJ01AB1234", char_confidence=[0.9] * 10)
    result = match(reading, store, SETTINGS)
    assert result.matched


# --- confidence genuinely changes the outcome --------------------------------


def test_confidence_moves_the_result_across_a_band_boundary() -> None:
    # Two I/L substitutions in the series (an alpha position both directions,
    # so neither edit gets the digit/alpha positional discount) -- large
    # enough a distance that confidence's effect on cost is not swamped by
    # the confusion-class discount alone, unlike the single-edit accuracy-
    # table cases above.
    store = _store_with("GJ01LI1234")

    confident = match(_reading("GJ01IL1234", confidences=[1.0] * 10), store, SETTINGS)
    unsure = match(_reading("GJ01IL1234", confidences=[0.0] * 10), store, SETTINGS)

    assert confident.matched and unsure.matched
    assert confident.explanation.final_score < unsure.explanation.final_score
    # The actual claim: this is not just "a slightly different number", it
    # crosses a band a human officer would read differently.
    assert confident.band == common_pb2.CONFIDENCE_BAND_PROBABLE
    assert unsure.band == common_pb2.CONFIDENCE_BAND_CONFIRMED


# --- MatchExplanation content -------------------------------------------------


def test_explanation_is_fully_populated_on_a_match() -> None:
    store = _store_with("GJ01AB1234")
    result = match(_reading("GJ01AB1Z34", confidences=[0.9] * 10), store, SETTINGS)

    assert result.matched
    explanation = result.explanation
    assert explanation.observed_plate == "GJ01AB1Z34"
    assert explanation.matched_plate == "GJ01AB1234"
    assert len(explanation.edits) >= 1
    assert explanation.weighted_distance > 0.0
    assert 0.0 <= explanation.format_plausibility <= 1.0
    assert 0.0 <= explanation.final_score <= 1.0


def test_exact_match_has_perfect_plausibility_and_zero_distance() -> None:
    store = _store_with("GJ01AB1234")
    result = match(_reading("GJ01AB1234"), store, SETTINGS)
    assert result.matched
    assert result.explanation.weighted_distance == 0.0
    assert result.explanation.format_plausibility == 1.0
    assert result.band == common_pb2.CONFIDENCE_BAND_CONFIRMED


# --- expiry, candidate selection, live reload --------------------------------


def test_expired_entry_is_not_matched() -> None:
    from datetime import UTC, datetime, timedelta

    from google.protobuf.timestamp_pb2 import Timestamp

    expired = Timestamp()
    expired.FromDatetime(datetime.now(UTC) - timedelta(days=1))

    watchlist = Watchlist()
    watchlist.add(events_pb2.WatchlistEntry(entry_id="E1", plate="GJ01AB1234", expires_at=expired))
    bloom = BloomFilter(expected_items=100)
    for skel in watchlist.skeletons():
        bloom.add(skel)
    store = WatchlistStore(watchlist, bloom)

    result = match(_reading("GJ01AB1234"), store, SETTINGS)
    assert not result.matched


def test_best_of_several_candidates_sharing_a_bucket_wins() -> None:
    # GJ01AB1234 and GJ01AB1Z34 share a skeleton bucket (both fold to the same
    # string). An observed reading equal to one of them exactly must prefer
    # the exact one over the confusable one, not pick arbitrarily.
    store = _store_with("GJ01AB1234", "GJ01AB1Z34")
    result = match(_reading("GJ01AB1234"), store, SETTINGS)
    assert result.matched
    assert result.entry.plate == "GJ01AB1234"


def test_watchlist_store_reload_is_visible_to_the_next_match() -> None:
    # This is the mechanism `/api/v1/watchlist/reload` relies on: the servicer
    # and the reload endpoint share one WatchlistStore, so a swap is visible
    # to the very next detection without restarting anything.
    store = _store_with("GJ01AB1234")
    assert not match(_reading("GJ05CD5678"), store, SETTINGS).matched

    new_store = _store_with("GJ05CD5678")
    store.replace(new_store.watchlist, new_store.bloom)

    assert match(_reading("GJ05CD5678"), store, SETTINGS).matched
    assert not match(_reading("GJ01AB1234"), store, SETTINGS).matched


def test_snapshot_pairs_watchlist_and_bloom_that_can_never_be_observed_torn() -> None:
    # P5: `store.watchlist` and `store.bloom` used to be two separate
    # attributes, replaced by two separate assignments in `replace()`. A
    # reload landing between a caller's read of one and its read of the
    # other could pair a fresh bloom filter with the stale index (or vice
    # versa) -- `snapshot()` must hand back one object holding both, taken
    # with a single read, so that can no longer happen.
    store = _store_with("GJ01AB1234")
    before = store.snapshot()
    assert before.watchlist is store.watchlist
    assert before.bloom is store.bloom

    new_store = _store_with("GJ05CD5678")
    store.replace(new_store.watchlist, new_store.bloom)

    # `before` still refers to the old pair, untouched by the reload --
    # proof that `replace()` swapped in a new snapshot object rather than
    # mutating the fields of the one `before` is holding.
    assert before.watchlist is not store.snapshot().watchlist
    assert before.bloom is not store.snapshot().bloom

    after = store.snapshot()
    assert after.watchlist is new_store.watchlist
    assert after.bloom is new_store.bloom
