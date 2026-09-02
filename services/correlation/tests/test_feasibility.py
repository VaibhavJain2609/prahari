from __future__ import annotations

from prahari_correlation.feasibility import check_feasibility, haversine_km

# Ahmedabad and Surat, roughly 208 km apart by great-circle distance (the
# ~260 km road distance is not what haversine measures -- see
# feasibility.py's module docstring on that known gap).
_AHMEDABAD = (23.0225, 72.5714)
_SURAT = (21.1702, 72.8311)


def test_haversine_known_distance_is_approximately_right() -> None:
    distance = haversine_km(*_AHMEDABAD, *_SURAT)
    assert 200 < distance < 215


def test_haversine_same_point_is_zero() -> None:
    assert haversine_km(23.0, 72.0, 23.0, 72.0) == 0.0


def test_slow_plausible_hop_is_feasible() -> None:
    # ~208 km in 3 hours -- ~69 km/h, well under the 120 km/h envelope.
    verdict = check_feasibility(*_AHMEDABAD, *_SURAT, elapsed_s=3 * 3600, max_speed_kmh=120.0)
    assert verdict.feasible
    assert verdict.implied_speed_kmh is not None
    assert 65 < verdict.implied_speed_kmh < 75


def test_impossible_hop_is_rejected() -> None:
    # The concrete case from DAY3-DESIGN.md: 200+ km in 3 minutes.
    verdict = check_feasibility(*_AHMEDABAD, *_SURAT, elapsed_s=180, max_speed_kmh=120.0)
    assert not verdict.feasible
    assert verdict.implied_speed_kmh is not None
    assert verdict.implied_speed_kmh > 4000


def test_hop_at_exactly_the_speed_envelope_is_feasible() -> None:
    distance = haversine_km(*_AHMEDABAD, *_SURAT)
    elapsed_s = (distance / 120.0) * 3600.0
    verdict = check_feasibility(*_AHMEDABAD, *_SURAT, elapsed_s=elapsed_s, max_speed_kmh=120.0)
    assert verdict.feasible


def test_same_location_zero_elapsed_time_is_feasible() -> None:
    verdict = check_feasibility(23.0, 72.0, 23.0, 72.0, elapsed_s=0.0, max_speed_kmh=120.0)
    assert verdict.feasible
    assert verdict.implied_speed_kmh is None


def test_different_location_zero_elapsed_time_is_a_teleport_and_rejected() -> None:
    verdict = check_feasibility(*_AHMEDABAD, *_SURAT, elapsed_s=0.0, max_speed_kmh=120.0)
    assert not verdict.feasible
    assert verdict.implied_speed_kmh is None


def test_negative_elapsed_time_with_distance_is_rejected() -> None:
    # Out-of-order timestamps should never occur once callers sort hops, but
    # the function itself must not divide by a negative number and produce a
    # nonsense negative speed that happens to pass the envelope.
    verdict = check_feasibility(*_AHMEDABAD, *_SURAT, elapsed_s=-60.0, max_speed_kmh=120.0)
    assert not verdict.feasible
