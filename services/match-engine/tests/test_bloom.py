"""bloom.py: the funnel's stage-1 gate. Its one non-negotiable property is no
false negatives -- anything added must always test positive, indefinitely, or
a genuine watchlist hit silently vanishes before it is ever scored.
"""

from __future__ import annotations

import pytest

from prahari_match.bloom import BloomFilter


def test_rejects_bad_construction_arguments() -> None:
    with pytest.raises(ValueError):
        BloomFilter(expected_items=0)
    with pytest.raises(ValueError):
        BloomFilter(expected_items=-5)
    with pytest.raises(ValueError):
        BloomFilter(expected_items=100, target_fp_rate=0.0)
    with pytest.raises(ValueError):
        BloomFilter(expected_items=100, target_fp_rate=1.0)


def test_never_produces_a_false_negative() -> None:
    # The one invariant everything else in the funnel depends on: every added
    # skeleton must always test positive, no matter how many others share the
    # filter with it.
    bloom = BloomFilter(expected_items=1000, target_fp_rate=0.01)
    skeletons = [f"6J01A8{i:04d}" for i in range(1000)]
    for skel in skeletons:
        bloom.add(skel)
    for skel in skeletons:
        assert skel in bloom, f"false negative on {skel!r} -- this must never happen"


def test_empty_filter_rejects_everything() -> None:
    bloom = BloomFilter(expected_items=100)
    assert "GJ01AB1234" not in bloom
    assert len(bloom) == 0
    assert bloom.current_false_positive_rate == 0.0


def test_len_counts_additions_including_duplicates() -> None:
    # A duplicate add is a no-op on the bit array but still reflects watchlist
    # size, which is what the false-positive-rate estimate is measured against.
    bloom = BloomFilter(expected_items=100)
    bloom.add("GJ01AB1234")
    bloom.add("GJ01AB1234")
    assert len(bloom) == 2


def test_false_positive_rate_is_honest_about_overfill() -> None:
    # Sizing the filter for far fewer items than are actually inserted must
    # show up as a visibly worse rate, not a rate that silently stays at the
    # original target.
    bloom = BloomFilter(expected_items=10, target_fp_rate=0.01)
    for i in range(2000):
        bloom.add(f"OVERFILL{i:06d}")
    assert bloom.current_false_positive_rate > 0.01


def test_false_positive_rate_near_target_at_expected_load() -> None:
    bloom = BloomFilter(expected_items=5000, target_fp_rate=0.01)
    for i in range(5000):
        bloom.add(f"ITEM{i:06d}")
    # Not required to be exact -- only close enough that the sizing formula is
    # doing its job rather than producing a wildly different rate.
    assert bloom.current_false_positive_rate < 0.05


def test_sizing_properties_are_positive() -> None:
    bloom = BloomFilter(expected_items=1000, target_fp_rate=0.01)
    assert bloom.size_bits > 0
    assert bloom.hash_count >= 1


def test_measured_false_positive_rate_is_plausible() -> None:
    # Empirically sample items never added and confirm the OBSERVED rate is in
    # the right ballpark of what `current_false_positive_rate` predicts --
    # this is what makes the property "honest" rather than just a formula.
    bloom = BloomFilter(expected_items=1000, target_fp_rate=0.02)
    for i in range(1000):
        bloom.add(f"WATCHLIST{i:06d}")

    probes = 20_000
    false_positives = sum(1 for i in range(probes) if f"NEVER-ADDED{i:06d}" in bloom)
    measured_rate = false_positives / probes

    predicted = bloom.current_false_positive_rate
    # Loose bound: sampling noise at this probe count is real, but a filter
    # that is off by an order of magnitude from its own prediction indicates
    # the sizing math (not just noise) is wrong.
    assert measured_rate < predicted * 5 + 0.01
