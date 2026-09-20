"""Spatio-temporal feasibility gating (DAY3-DESIGN.md §3.2): reject a hop
between two detections of the same plate when the implied average speed is
physically implausible -- the concrete case being "200 km in 3 minutes".

**Known limitation, stated plainly, not hidden**: `haversine_km` is
great-circle distance, not road-network distance. `correlation-engineer.md`
asks for road-network distance; a real routing engine (OSRM/Valhalla) is out
of scope for the hackathon's time budget. Road distance is always >= the
great-circle distance between the same two points, so this gate is
conservative in the *safe* direction: it can pass a hop a road engine would
reject, but it can never reject a hop that was actually feasible. That
asymmetry belongs in `docs/SCALE-80K.md`'s limitations section too, not only
here.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt

__all__ = ["FeasibilityVerdict", "haversine_km", "check_feasibility"]

_EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_r, lon1_r, lat2_r, lon2_r = map(radians, (lat1, lon1, lat2, lon2))
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = sin(dlat / 2) ** 2 + cos(lat1_r) * cos(lat2_r) * sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * asin(sqrt(a))


@dataclass(frozen=True)
class FeasibilityVerdict:
    feasible: bool
    distance_km: float
    elapsed_s: float
    implied_speed_kmh: float | None
    """`None` when `elapsed_s <= 0` -- speed is undefined over zero or
    negative elapsed time. That is only feasible when no distance needed to
    be covered either (the same location, same instant); any non-zero
    distance in zero time is a teleport and is rejected."""
    reason: str | None = None
    """Why the hop failed, when it did: "teleport" (non-zero distance in ~no
    time), "negative_elapsed" (timestamps out of order beyond the clock-skew
    allowance -- a data problem, not a speed problem). `None` on feasible
    verdicts and on plain over-the-speed-envelope rejections, which callers
    describe themselves ("exceeds max_speed_kmh ...")."""


def check_feasibility(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
    elapsed_s: float,
    max_speed_kmh: float,
    *,
    clock_skew_allowance_s: float = 0.0,
) -> FeasibilityVerdict:
    distance_km = haversine_km(lat1, lon1, lat2, lon2)

    if elapsed_s < -clock_skew_allowance_s:
        # Timestamps out of order by MORE than two cameras' clocks can
        # plausibly disagree -- this is not a fast vehicle or a teleport, it
        # is broken data (a poisoned or mis-synced `observed_at`), and the
        # rejection is labelled as such rather than as an impossible speed.
        return FeasibilityVerdict(
            feasible=False,
            distance_km=distance_km,
            elapsed_s=elapsed_s,
            implied_speed_kmh=None,
            reason="negative_elapsed",
        )

    if elapsed_s <= 0:
        # Within the skew allowance (or exactly zero): treated as "no time to
        # have moved in" -- feasible only if no distance needed covering
        # either (same spot, same instant). A non-zero distance in zero or
        # skew-small negative elapsed time is a teleport, not a fast vehicle.
        return FeasibilityVerdict(
            feasible=distance_km <= 1e-6,
            distance_km=distance_km,
            elapsed_s=elapsed_s,
            implied_speed_kmh=None,
            reason=None if distance_km <= 1e-6 else "teleport",
        )

    implied_speed_kmh = distance_km / (elapsed_s / 3600.0)
    return FeasibilityVerdict(
        feasible=implied_speed_kmh <= max_speed_kmh,
        distance_km=distance_km,
        elapsed_s=elapsed_s,
        implied_speed_kmh=implied_speed_kmh,
    )
