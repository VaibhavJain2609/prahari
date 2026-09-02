"""Route assembly (DAY3-DESIGN.md §3.4): turn one plate's stored sightings
into an ordered, physically-defensible route.

`hops` is the *connected* route -- consecutive sightings that chain together
either via an appearance-bridged waypoint (`link_kind="bridged"`,
DAY3-DESIGN.md §3.3) or, when no such waypoint exists, a direct hop that
passed feasibility gating (`link_kind="plate"`). Bridging is attempted
*before* falling back to a plain direct check, not only as a rescue for a
failed one: by the triangle inequality, any waypoint whose two legs are both
individually feasibility-gated makes the direct hop between its endpoints
feasible too (leg distances can only sum to *more* than the direct
great-circle distance), so an infeasible direct hop can never be rescued by
a bridge after the fact -- bridging has to be tried first to ever have an
effect. A sighting that cannot be chained to the growing route -- direct hop
infeasible, and no bridging waypoint available -- is never silently folded
in as if it were a normal step; it is excluded from `hops` and recorded in
`rejected` instead, with the reason and the speed the direct hop would have
implied. Evidence is never discarded outright: it is just not misrepresented
as one continuous leg of the same journey. The next candidate sighting is
still tested against the last *connected* hop, not against the rejected one,
so one anomalous reading does not fracture the rest of an otherwise-
continuous route.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from prahari.v1 import events_pb2

from .bridging import cosine_similarity, is_bridge_candidate
from .feasibility import check_feasibility
from .registry_client import DarkZone, GeoPoint, RegistryClient
from .store import DetectionStore, plate_key, wall_clock_s

__all__ = ["LinkKind", "RouteHop", "RejectedHop", "RouteResult", "build_route"]


class LinkKind(StrEnum):
    PLATE = "plate"
    BRIDGED = "bridged"


@dataclass(frozen=True)
class RouteHop:
    camera_id: str
    location: GeoPoint | None
    wall_clock_s: float
    pts_ms: int
    link_kind: LinkKind | None
    """`None` only for the route's first hop -- there is no preceding leg to
    describe a link kind for."""
    confidence: float
    evidence_ref: str


@dataclass(frozen=True)
class RejectedHop:
    from_camera_id: str
    to_camera_id: str
    reason: str
    implied_speed_kmh: float | None


@dataclass(frozen=True)
class RouteResult:
    plate: str
    hops: list[RouteHop]
    rejected: list[RejectedHop]
    dark_zones: list[DarkZone]
    """The registry's current full dark-zone list, not filtered to zones
    plausibly on this specific route -- precise corridor matching needs a
    road-network model this service deliberately does not have (see
    `feasibility.py`'s module docstring). Handing the console the full list
    alongside the route's own coordinates is an honest, stated simplification,
    not a hidden gap."""


def _hop_from_detection(
    detection: events_pb2.VehicleDetection,
    location: GeoPoint | None,
    link_kind: LinkKind | None,
    confidence: float,
) -> RouteHop:
    return RouteHop(
        camera_id=detection.camera_id,
        location=location,
        wall_clock_s=wall_clock_s(detection),
        pts_ms=detection.observed_at.pts_ms,
        link_kind=link_kind,
        confidence=confidence,
        evidence_ref=detection.evidence_ref,
    )


async def _find_bridge(
    store: DetectionStore,
    registry: RegistryClient,
    left: events_pb2.VehicleDetection,
    left_loc: GeoPoint,
    right: events_pb2.VehicleDetection,
    right_loc: GeoPoint,
    max_speed_kmh: float,
    appearance_threshold: float,
) -> tuple[events_pb2.VehicleDetection, GeoPoint, float] | None:
    """The first plate-unreadable detection, chronologically between `left`
    and `right`, that is both feasibility-gated against *both* neighbours and
    appearance-similar to both. Returns the candidate, its location, and the
    bridge confidence (the weaker of the two cosine similarities), or `None`
    if nothing in the store closes the gap."""
    for candidate in sorted(
        store.unplated_between(wall_clock_s(left), wall_clock_s(right)), key=wall_clock_s
    ):
        if not candidate.appearance_embedding:
            continue
        candidate_loc = await registry.camera_location(candidate.camera_id)
        if candidate_loc is None:
            continue

        left_leg = check_feasibility(
            left_loc.latitude,
            left_loc.longitude,
            candidate_loc.latitude,
            candidate_loc.longitude,
            wall_clock_s(candidate) - wall_clock_s(left),
            max_speed_kmh,
        )
        right_leg = check_feasibility(
            candidate_loc.latitude,
            candidate_loc.longitude,
            right_loc.latitude,
            right_loc.longitude,
            wall_clock_s(right) - wall_clock_s(candidate),
            max_speed_kmh,
        )
        if not (left_leg.feasible and right_leg.feasible):
            continue

        if not is_bridge_candidate(
            list(left.appearance_embedding),
            list(candidate.appearance_embedding),
            list(right.appearance_embedding),
            appearance_threshold,
        ):
            continue

        confidence = min(
            cosine_similarity(
                list(left.appearance_embedding), list(candidate.appearance_embedding)
            ),
            cosine_similarity(
                list(candidate.appearance_embedding), list(right.appearance_embedding)
            ),
        )
        return candidate, candidate_loc, confidence

    return None


async def build_route(
    raw_plate_text: str,
    store: DetectionStore,
    registry: RegistryClient,
    max_speed_kmh: float,
    appearance_threshold: float,
) -> RouteResult:
    key = plate_key(raw_plate_text)
    detections = store.by_plate(raw_plate_text)
    dark_zones = await registry.dark_zones()

    if not detections:
        return RouteResult(plate=key, hops=[], rejected=[], dark_zones=dark_zones)

    hops: list[RouteHop] = []
    rejected: list[RejectedHop] = []

    first = detections[0]
    prev = first
    prev_loc = await registry.camera_location(first.camera_id)
    hops.append(_hop_from_detection(first, prev_loc, link_kind=None, confidence=1.0))

    for current in detections[1:]:
        current_loc = await registry.camera_location(current.camera_id)

        if prev_loc is None or current_loc is None:
            # Cannot feasibility-gate without both endpoints' locations --
            # pass the sighting through rather than discarding a real,
            # plate-confirmed detection because the registry does not know
            # where a camera is. Confidence stays full: the *sighting* is not
            # in doubt, only the untested link to it.
            hops.append(_hop_from_detection(current, current_loc, LinkKind.PLATE, 1.0))
            prev, prev_loc = current, current_loc
            continue

        bridge = await _find_bridge(
            store,
            registry,
            prev,
            prev_loc,
            current,
            current_loc,
            max_speed_kmh,
            appearance_threshold,
        )
        if bridge is not None:
            candidate, candidate_loc, confidence = bridge
            hops.append(_hop_from_detection(candidate, candidate_loc, LinkKind.BRIDGED, confidence))
            hops.append(_hop_from_detection(current, current_loc, LinkKind.BRIDGED, confidence))
            prev, prev_loc = current, current_loc
            continue

        elapsed = wall_clock_s(current) - wall_clock_s(prev)
        verdict = check_feasibility(
            prev_loc.latitude,
            prev_loc.longitude,
            current_loc.latitude,
            current_loc.longitude,
            elapsed,
            max_speed_kmh,
        )
        if verdict.feasible:
            hops.append(_hop_from_detection(current, current_loc, LinkKind.PLATE, 1.0))
            prev, prev_loc = current, current_loc
            continue

        rejected.append(
            RejectedHop(
                from_camera_id=prev.camera_id,
                to_camera_id=current.camera_id,
                reason="exceeds max_speed_kmh with no appearance bridge available",
                implied_speed_kmh=verdict.implied_speed_kmh,
            )
        )
        # `prev`/`prev_loc` deliberately unchanged: the next candidate is
        # tested against the last *connected* hop, not the rejected one.

    return RouteResult(plate=key, hops=hops, rejected=rejected, dark_zones=dark_zones)
