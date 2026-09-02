"""confusion.py: the model everything else in the funnel trusts.

If `skeleton()` or `substitution_cost()` is wrong, the Bloom filter rejects
genuine hits (a false negative nothing downstream can recover from) or the
scorer rewards nonsense. These tests pin the exact behaviour DAY2-DESIGN.md
§7.1/§7.2 describes, character by character.
"""

from __future__ import annotations

import pytest

from prahari_match.confusion import (
    in_same_confusion_class,
    skeleton,
    substitution_cost,
    weighted_levenshtein,
)

# Every bidirectional confusion class the model claims to know about.
CONFUSION_PAIRS = [
    ("0", "O"),
    ("0", "D"),
    ("0", "Q"),
    ("O", "D"),
    ("8", "B"),
    ("1", "I"),
    ("1", "L"),
    ("I", "L"),
    ("5", "S"),
    ("2", "Z"),
    ("6", "G"),
]


@pytest.mark.parametrize("a,b", CONFUSION_PAIRS)
def test_confusion_pairs_are_bidirectional(a: str, b: str) -> None:
    assert in_same_confusion_class(a, b)
    assert in_same_confusion_class(b, a)


def test_identical_characters_are_not_a_confusion() -> None:
    # A character never needs "correcting" into itself -- this is not a
    # substitution at all, and must not be priced or flagged as one.
    assert not in_same_confusion_class("0", "0")


@pytest.mark.parametrize("a,b", [("0", "W"), ("A", "Z"), ("3", "4"), ("8", "S")])
def test_unrelated_characters_are_not_a_confusion(a: str, b: str) -> None:
    assert not in_same_confusion_class(a, b)


def test_skeleton_folds_the_day2_design_example() -> None:
    # DAY2-DESIGN.md §7.2: this exact pair is the worked example for why stage
    # 1 buckets on skeleton rather than raw text.
    assert skeleton("GJ01AB1Z34") == skeleton("GJ01AB1234") == "6J01A81234"


def test_skeleton_is_a_pure_function_of_confusion_class() -> None:
    # Every character in a class folds to the same representative, whichever
    # member appears -- otherwise two OCR misreads of the same true plate
    # could still land in different buckets.
    for pair in CONFUSION_PAIRS:
        a, b = pair
        assert skeleton(a) == skeleton(b)


def test_skeleton_leaves_unconfusable_characters_untouched() -> None:
    assert skeleton("JK") == "JK"  # neither letter belongs to any confusion class
    assert skeleton("9") == "9"  # 9 is in no confusion class


class TestSubstitutionCost:
    def test_identical_characters_cost_nothing(self) -> None:
        cost, known = substitution_cost("A", "A", confidence=1.0)
        assert cost == 0.0
        assert known is False

    def test_confusable_pair_is_cheaper_than_unrelated_pair(self) -> None:
        confusable_cost, confusable_known = substitution_cost("0", "O", confidence=0.5)
        unrelated_cost, unrelated_known = substitution_cost("0", "W", confidence=0.5)
        assert confusable_known is True
        assert unrelated_known is False
        assert confusable_cost < unrelated_cost

    def test_lower_confidence_makes_any_substitution_cheaper(self) -> None:
        # Low OCR confidence IS the model telling us it may have guessed --
        # so ANY substitution at that position should get cheaper, not just
        # a known-confusion one.
        high_conf_cost, _ = substitution_cost("0", "W", confidence=0.95)
        low_conf_cost, _ = substitution_cost("0", "W", confidence=0.05)
        assert low_conf_cost < high_conf_cost

    def test_confidence_floor_prevents_zero_cost(self) -> None:
        # A zero-confidence character must not make a substitution free --
        # that would let one garbage character absorb an arbitrary edit at no
        # cost, defeating the whole distance metric.
        cost, _ = substitution_cost("0", "W", confidence=0.0)
        assert cost > 0.0

    def test_positional_mask_discounts_a_class_crossing_swap(self) -> None:
        from prahari_common.plates import MASK_ALPHA, MASK_DIGIT

        # "0" read where the watchlist plate has "O" (both in the same
        # confusion class) is near-certain OCR confusion specifically because
        # a digit and a letter look alike -- so a mask that expects a letter
        # here should cost less than a mask that expects a digit.
        letter_mask_cost, _ = substitution_cost("0", "O", confidence=0.5, mask_char=MASK_ALPHA)
        digit_mask_cost, _ = substitution_cost("0", "O", confidence=0.5, mask_char=MASK_DIGIT)
        assert letter_mask_cost < digit_mask_cost

    def test_no_mask_still_prices_the_confusion(self) -> None:
        # A NONCONFORMING candidate carries no mask at all; this must not
        # raise, and should fall back to the plain confusion-class cost.
        cost, known = substitution_cost("0", "O", confidence=0.5, mask_char=None)
        assert known is True
        assert cost > 0.0


class TestWeightedLevenshtein:
    def test_identical_strings_have_zero_distance_and_no_edits(self) -> None:
        result = weighted_levenshtein("GJ01AB1234", "GJ01AB1234")
        assert result.distance == 0.0
        assert result.edits == ()

    def test_single_confusable_substitution(self) -> None:
        result = weighted_levenshtein("GJ01AB1Z34", "GJ01AB1234", mask="AA99AA9999")
        assert len(result.edits) == 1
        edit = result.edits[0]
        assert edit.observed == "Z"
        assert edit.matched == "2"
        assert edit.is_known_confusion is True
        assert edit.cost > 0.0

    def test_dropped_character_is_a_deletion_not_a_substitution(self) -> None:
        # Watchlist has one character OCR never read at all -- the cheapest
        # explanation is a deletion, not a substitution against an unrelated
        # trailing character.
        result = weighted_levenshtein("GJ01AB123", "GJ01AB1234")
        kinds = {e.matched for e in result.edits if e.observed == ""}
        assert "4" in kinds

    def test_confidence_reduces_cost_of_the_edit_it_applies_to(self) -> None:
        low_conf = weighted_levenshtein(
            "GJ01AB1Z34", "GJ01AB1234", confidences=(1, 1, 1, 1, 1, 1, 1, 0.0, 1, 1)
        )
        high_conf = weighted_levenshtein(
            "GJ01AB1Z34", "GJ01AB1234", confidences=(1, 1, 1, 1, 1, 1, 1, 1.0, 1, 1)
        )
        assert low_conf.distance < high_conf.distance

    def test_missing_confidence_defaults_to_untrusted(self) -> None:
        # No confidence supplied at all must not raise, and should behave like
        # every position is untrusted (cheapest possible substitutions).
        with_no_confidence = weighted_levenshtein("GJ01AB1Z34", "GJ01AB1234")
        with_zero_confidence = weighted_levenshtein(
            "GJ01AB1Z34", "GJ01AB1234", confidences=(0.0,) * 10
        )
        assert with_no_confidence.distance == with_zero_confidence.distance

    def test_edit_positions_index_the_matched_string(self) -> None:
        result = weighted_levenshtein("GJ01AB1Z34", "GJ01AB1234")
        edit = result.edits[0]
        assert "GJ01AB1234"[edit.position] == edit.matched
