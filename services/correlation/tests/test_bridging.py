from __future__ import annotations

from prahari_correlation.bridging import cosine_similarity, is_bridge_candidate


def test_identical_vectors_are_maximally_similar() -> None:
    assert cosine_similarity([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == 1.0


def test_orthogonal_vectors_are_zero_similarity() -> None:
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_scale_invariant() -> None:
    # Cosine similarity ignores magnitude -- a brighter/darker crop of the
    # same vehicle must not look less similar than an identically-lit one.
    a = [1.0, 2.0, 3.0]
    b = [2.0, 4.0, 6.0]
    assert abs(cosine_similarity(a, b) - 1.0) < 1e-9


def test_mismatched_length_is_zero_not_a_crash() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0


def test_empty_vector_is_zero_not_a_crash() -> None:
    assert cosine_similarity([], [1.0, 0.0]) == 0.0


def test_zero_vector_is_zero_not_a_division_error() -> None:
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_bridge_requires_both_sides_to_clear_the_threshold() -> None:
    left = [1.0, 0.0]
    middle_similar_to_left_only = [1.0, 0.0]
    right = [0.0, 1.0]
    assert not is_bridge_candidate(left, middle_similar_to_left_only, right, threshold=0.85)


def test_bridge_accepted_when_both_sides_clear_the_threshold() -> None:
    left = [1.0, 0.05]
    middle = [1.0, 0.0]
    right = [1.0, 0.02]
    assert is_bridge_candidate(left, middle, right, threshold=0.9)
