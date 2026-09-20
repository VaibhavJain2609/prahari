from __future__ import annotations

from datetime import UTC, datetime

from google.protobuf.timestamp_pb2 import Timestamp
from prahari.v1 import common_pb2, events_pb2

from prahari_correlation.registry_client import DarkZone, GeoPoint
from prahari_correlation.routes import LinkKind, build_route
from prahari_correlation.store import DetectionStore

MAX_SPEED_KMH = 120.0
APPEARANCE_THRESHOLD = 0.85

CAM_A = GeoPoint(latitude=0.0, longitude=0.0)
CAM_B = GeoPoint(latitude=0.0, longitude=0.05)  # ~5.55km east of CAM_A
CAM_C = GeoPoint(latitude=0.0, longitude=0.10)  # ~11.1km east of CAM_A, ~5.55km east of CAM_B

_counter = 0


def _detection(
    camera_id: str,
    *,
    plate_text: str | None = None,
    wall_clock_s_value: float = 1000.0,
    appearance_embedding: list[float] | None = None,
) -> events_pb2.VehicleDetection:
    global _counter
    _counter += 1
    ts = Timestamp()
    ts.FromDatetime(datetime.fromtimestamp(wall_clock_s_value, tz=UTC))
    kwargs = {}
    if plate_text is not None:
        kwargs["plate"] = events_pb2.PlateReading(normalised_text=plate_text)
    if appearance_embedding is not None:
        kwargs["appearance_embedding"] = appearance_embedding
    return events_pb2.VehicleDetection(
        detection_id=f"D{_counter}",
        camera_id=camera_id,
        observed_at=common_pb2.StreamTime(wall_clock=ts),
        evidence_ref=f"evidence/{_counter}",
        **kwargs,
    )


class _FakeRegistry:
    def __init__(
        self,
        locations: dict[str, GeoPoint | None],
        dark_zones: list[DarkZone] | None = None,
    ) -> None:
        self._locations = locations
        self._dark_zones = dark_zones or []

    async def camera_location(self, camera_id: str) -> GeoPoint | None:
        return self._locations.get(camera_id)

    async def dark_zones(self) -> list[DarkZone]:
        return self._dark_zones


async def test_no_detections_is_an_empty_route() -> None:
    store = DetectionStore(max_per_plate=10, max_plates=10)
    registry = _FakeRegistry({})

    result = await build_route("GJ01AB1234", store, registry, MAX_SPEED_KMH, APPEARANCE_THRESHOLD)

    assert result.hops == []
    assert result.rejected == []


async def test_single_detection_is_one_hop_with_no_link_kind() -> None:
    store = DetectionStore(max_per_plate=10, max_plates=10)
    store.add(_detection("CAM-A", plate_text="GJ01AB1234", wall_clock_s_value=1000.0))
    registry = _FakeRegistry({"CAM-A": CAM_A})

    result = await build_route("GJ01AB1234", store, registry, MAX_SPEED_KMH, APPEARANCE_THRESHOLD)

    assert len(result.hops) == 1
    assert result.hops[0].camera_id == "CAM-A"
    assert result.hops[0].link_kind is None
    assert result.hops[0].confidence == 1.0


async def test_two_feasible_detections_with_no_bridge_candidate_link_as_plate() -> None:
    store = DetectionStore(max_per_plate=10, max_plates=10)
    store.add(_detection("CAM-A", plate_text="GJ01AB1234", wall_clock_s_value=1000.0))
    store.add(_detection("CAM-C", plate_text="GJ01AB1234", wall_clock_s_value=1660.0))  # +660s
    registry = _FakeRegistry({"CAM-A": CAM_A, "CAM-C": CAM_C})

    result = await build_route("GJ01AB1234", store, registry, MAX_SPEED_KMH, APPEARANCE_THRESHOLD)

    assert [h.camera_id for h in result.hops] == ["CAM-A", "CAM-C"]
    assert result.hops[1].link_kind == LinkKind.PLATE
    assert result.hops[1].confidence == 1.0
    assert result.rejected == []


async def test_infeasible_pair_with_no_bridge_is_rejected_not_silently_folded_in() -> None:
    store = DetectionStore(max_per_plate=10, max_plates=10)
    store.add(_detection("CAM-A", plate_text="GJ01AB1234", wall_clock_s_value=1000.0))
    store.add(_detection("CAM-C", plate_text="GJ01AB1234", wall_clock_s_value=1060.0))  # +60s only
    registry = _FakeRegistry({"CAM-A": CAM_A, "CAM-C": CAM_C})

    result = await build_route("GJ01AB1234", store, registry, MAX_SPEED_KMH, APPEARANCE_THRESHOLD)

    assert [h.camera_id for h in result.hops] == ["CAM-A"]
    assert len(result.rejected) == 1
    assert result.rejected[0].from_camera_id == "CAM-A"
    assert result.rejected[0].to_camera_id == "CAM-C"
    assert result.rejected[0].implied_speed_kmh is not None
    assert result.rejected[0].implied_speed_kmh > MAX_SPEED_KMH


async def test_a_feasible_appearance_matched_waypoint_is_used_as_a_bridge() -> None:
    # Both legs individually feasible (and therefore, by the triangle
    # inequality, the direct hop is feasible too) -- bridging here is
    # enrichment, not rescue: a real corroborating waypoint exists, so it is
    # surfaced instead of silently skipped in favour of the plain direct hop.
    embedding = [1.0, 0.0, 0.0]
    store = DetectionStore(max_per_plate=10, max_plates=10)
    store.add(
        _detection(
            "CAM-A",
            plate_text="GJ01AB1234",
            wall_clock_s_value=1000.0,
            appearance_embedding=embedding,
        )
    )
    store.add(
        _detection("CAM-B", wall_clock_s_value=1330.0, appearance_embedding=embedding)
    )  # unplated, halfway
    store.add(
        _detection(
            "CAM-C",
            plate_text="GJ01AB1234",
            wall_clock_s_value=1660.0,
            appearance_embedding=embedding,
        )
    )
    registry = _FakeRegistry({"CAM-A": CAM_A, "CAM-B": CAM_B, "CAM-C": CAM_C})

    result = await build_route("GJ01AB1234", store, registry, MAX_SPEED_KMH, APPEARANCE_THRESHOLD)

    assert [h.camera_id for h in result.hops] == ["CAM-A", "CAM-B", "CAM-C"]
    assert result.hops[1].link_kind == LinkKind.BRIDGED
    assert result.hops[2].link_kind == LinkKind.BRIDGED
    assert result.hops[1].confidence == 1.0
    assert result.rejected == []


async def test_unknown_camera_location_passes_through_ungated() -> None:
    store = DetectionStore(max_per_plate=10, max_plates=10)
    store.add(_detection("CAM-A", plate_text="GJ01AB1234", wall_clock_s_value=1000.0))
    store.add(_detection("CAM-UNMAPPED", plate_text="GJ01AB1234", wall_clock_s_value=1010.0))
    registry = _FakeRegistry({"CAM-A": CAM_A})  # CAM-UNMAPPED deliberately absent

    result = await build_route("GJ01AB1234", store, registry, MAX_SPEED_KMH, APPEARANCE_THRESHOLD)

    assert [h.camera_id for h in result.hops] == ["CAM-A", "CAM-UNMAPPED"]
    # The sighting is kept -- it is real and plate-confirmed -- but the link
    # to it was never feasibility-gated, and must say so rather than read
    # identically to a gated "plate" hop in the evidence export.
    assert result.hops[1].link_kind == LinkKind.UNVERIFIED
    assert result.hops[1].confidence == 1.0
    assert result.hops[1].location is None
    assert result.ungated_hops == 1
    assert result.rejected == []


async def test_consecutive_same_camera_detections_collapse_into_one_dwell_hop() -> None:
    # A vehicle in frame for 8 s at ~3 fps is ~24 detections; the route must
    # show ONE hop carrying the dwell interval, not 24 identical-camera hops.
    store = DetectionStore(max_per_plate=50, max_plates=10)
    for i in range(5):
        store.add(_detection("CAM-A", plate_text="GJ01AB1234", wall_clock_s_value=1000.0 + i))
    store.add(_detection("CAM-B", plate_text="GJ01AB1234", wall_clock_s_value=1600.0))
    registry = _FakeRegistry({"CAM-A": CAM_A, "CAM-B": CAM_B})

    result = await build_route("GJ01AB1234", store, registry, MAX_SPEED_KMH, APPEARANCE_THRESHOLD)

    assert [h.camera_id for h in result.hops] == ["CAM-A", "CAM-B"]
    dwell = result.hops[0]
    assert dwell.sightings == 5
    assert dwell.first_seen_s == 1000.0
    assert dwell.last_seen_s == 1004.0
    assert dwell.link_kind is None
    assert result.hops[1].link_kind == LinkKind.PLATE
    # The leg was timed from the END of the dwell (1004) to the first sighting
    # at CAM-B (1600): ~5.5km in 596s is feasible either way here, but the
    # store still holds all five raw sightings as evidence.
    assert len(store.by_plate("GJ01AB1234")) == 6


async def test_dwell_end_not_start_is_the_leg_departure_time() -> None:
    # ~5.55 km from CAM-A to CAM-B. If the leg were timed from the dwell's
    # FIRST sighting (1000s) the 660s gap would be feasible; timed honestly
    # from the LAST (1750s, while the vehicle was still in frame) the 10s
    # gap implies ~2000 km/h -- a rejection, not a hop.
    store = DetectionStore(max_per_plate=50, max_plates=10)
    for i in range(3):
        store.add(
            _detection("CAM-A", plate_text="GJ01AB1234", wall_clock_s_value=1000.0 + i * 375)
        )  # dwell spans 1000..1750
    store.add(_detection("CAM-B", plate_text="GJ01AB1234", wall_clock_s_value=1760.0))
    registry = _FakeRegistry({"CAM-A": CAM_A, "CAM-B": CAM_B})

    result = await build_route("GJ01AB1234", store, registry, MAX_SPEED_KMH, APPEARANCE_THRESHOLD)

    assert [h.camera_id for h in result.hops] == ["CAM-A"]
    assert len(result.rejected) == 1
    assert result.rejected[0].from_camera_id == "CAM-A"
    assert result.rejected[0].to_camera_id == "CAM-B"


async def test_non_consecutive_same_camera_is_not_collapsed() -> None:
    # A, B, A is a return journey, not a dwell -- only CONSECUTIVE
    # same-camera sightings fold.
    store = DetectionStore(max_per_plate=50, max_plates=10)
    store.add(_detection("CAM-A", plate_text="GJ01AB1234", wall_clock_s_value=1000.0))
    store.add(_detection("CAM-B", plate_text="GJ01AB1234", wall_clock_s_value=1600.0))
    store.add(_detection("CAM-A", plate_text="GJ01AB1234", wall_clock_s_value=2200.0))
    registry = _FakeRegistry({"CAM-A": CAM_A, "CAM-B": CAM_B})

    result = await build_route("GJ01AB1234", store, registry, MAX_SPEED_KMH, APPEARANCE_THRESHOLD)

    assert [h.camera_id for h in result.hops] == ["CAM-A", "CAM-B", "CAM-A"]
    assert all(h.sightings == 1 for h in result.hops)


async def test_a_rejected_middle_hop_does_not_fracture_the_rest_of_the_route() -> None:
    # The next candidate is tested against the last *connected* hop, not the
    # rejected one -- routes.py's own module docstring's headline claim.
    # CAM-C is infeasible from CAM-A (60s for ~11km); CAM-D is back at
    # CAM-A's own location, so it is trivially feasible *from CAM-A*, but
    # would not be reachable at all from the (rejected) CAM-C detection's
    # timestamp if the rejection had advanced `prev`.
    store = DetectionStore(max_per_plate=10, max_plates=10)
    store.add(_detection("CAM-A", plate_text="GJ01AB1234", wall_clock_s_value=1000.0))
    store.add(_detection("CAM-C", plate_text="GJ01AB1234", wall_clock_s_value=1060.0))  # rejected
    store.add(_detection("CAM-D", plate_text="GJ01AB1234", wall_clock_s_value=2000.0))
    registry = _FakeRegistry({"CAM-A": CAM_A, "CAM-C": CAM_C, "CAM-D": CAM_A})

    result = await build_route("GJ01AB1234", store, registry, MAX_SPEED_KMH, APPEARANCE_THRESHOLD)

    assert [h.camera_id for h in result.hops] == ["CAM-A", "CAM-D"]
    assert result.hops[1].link_kind == LinkKind.PLATE
    assert len(result.rejected) == 1
    assert result.rejected[0].from_camera_id == "CAM-A"
    assert result.rejected[0].to_camera_id == "CAM-C"


async def test_dark_zones_from_the_registry_are_propagated() -> None:
    store = DetectionStore(max_per_plate=10, max_plates=10)
    store.add(_detection("CAM-A", plate_text="GJ01AB1234", wall_clock_s_value=1000.0))
    dark_zones = [DarkZone(camera_id="CAM-DOWN", location=None)]
    registry = _FakeRegistry({"CAM-A": CAM_A}, dark_zones=dark_zones)

    result = await build_route("GJ01AB1234", store, registry, MAX_SPEED_KMH, APPEARANCE_THRESHOLD)

    assert result.dark_zones == dark_zones
