"""Gap analysis: row→model mapping and the few decisions these functions own.

The SQL itself (KNN over the GIST index, `FILTER` aggregates, the ltree scope
predicate) only means something against PostGIS; what these tests pin is the
contract around it — scope is always the first bind parameter, `coverage_pct`
is healthy-over-registered with the empty-district case pinned, a null
`distance_m` means "no healthy camera anywhere" rather than a big number, and
the `healthy_only` flag changes the query rather than filtering after the
fact.
"""

from __future__ import annotations

from datetime import UTC, datetime

from prahari_registry.gaps import (
    cameras_geojson,
    dark_zones,
    district_coverage,
    nearest_cameras,
)


class FakePool:
    """Serves one canned `fetch` result per call and records the query."""

    def __init__(self, *results: list[dict]) -> None:
        self.results = list(results)
        self.queries: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        return self.results.pop(0) if self.results else []


def _coverage_row(**overrides) -> dict:
    row = {
        "district": "Ahmedabad",
        "registered": 10,
        "healthy": 7,
        "degraded": 1,
        "unreachable": 1,
        "tampered": 0,
        "unknown": 1,
        "absent": 2,
    }
    row.update(overrides)
    return row


async def test_district_coverage_counts_degraded_as_not_coverage():
    """A camera delivering 2 fps of a covered lens is on the register and is
    not watching anything — coverage_pct is healthy over registered, nothing
    else."""
    pool = FakePool([_coverage_row()])
    [row] = await district_coverage(pool, scope="gj")
    assert row.district == "Ahmedabad"
    assert row.coverage_pct == 70.0
    assert row.absent == 2
    assert pool.queries[0][1] == ("gj",)


async def test_district_coverage_of_an_empty_district_is_zero_not_an_error():
    pool = FakePool([_coverage_row(district="Kutch", registered=0, healthy=0)])
    [row] = await district_coverage(pool, scope="gj")
    assert row.coverage_pct == 0.0


def _zone_row(**overrides) -> dict:
    row = {
        "id": "00000000-0000-0000-0000-0000000000ab",
        "site_name": "Zone 4 junction",
        "district": "Ahmedabad",
        "latitude": 23.0,
        "longitude": 72.5,
        "state": "unreachable",
        "reason": "no heartbeat",
        "distance_m": 812.34,
    }
    row.update(overrides)
    return row


async def test_dark_zones_rounds_the_nearest_healthy_distance():
    pool = FakePool([_zone_row()])
    [zone] = await dark_zones(pool, scope="gj", radius_m=500.0)
    assert zone.camera_id.endswith("0ab")
    assert zone.nearest_healthy_m == 812.3
    assert zone.state.value == "unreachable"
    assert pool.queries[0][1] == ("gj", 500.0)


async def test_dark_zones_null_distance_means_no_healthy_camera_anywhere():
    """Strictly worse than a large number — and must render as such, not as
    'nearest healthy camera is 0 m away'."""
    pool = FakePool([_zone_row(distance_m=None)])
    [zone] = await dark_zones(pool, scope="gj", radius_m=500.0)
    assert zone.nearest_healthy_m is None


async def test_nearest_cameras_maps_rows_and_binds_lon_before_lat():
    """`ST_MakePoint($2, $3)` takes (lon, lat) — a swap here silently searches
    the wrong hemisphere and nothing raises."""
    pool = FakePool([_zone_row(state="healthy")])
    [cam] = await nearest_cameras(pool, scope="gj", latitude=23.03, longitude=72.58, limit=5)
    assert cam.distance_m == 812.3
    assert pool.queries[0][1] == ("gj", 72.58, 23.03, 5)


async def test_nearest_cameras_healthy_only_changes_the_query_not_the_filter():
    healthy = FakePool([])
    everyone = FakePool([])
    await nearest_cameras(healthy, scope="gj", latitude=23.0, longitude=72.5)
    await nearest_cameras(everyone, scope="gj", latitude=23.0, longitude=72.5, healthy_only=False)
    assert "effective_health_state = 'healthy'" in healthy.queries[0][0]
    assert "effective_health_state = 'healthy'" not in everyone.queries[0][0]


def _geo_row(**overrides) -> dict:
    row = {
        "id": "00000000-0000-0000-0000-0000000000ab",
        "site_name": "Zone 4 junction",
        "district": "Ahmedabad",
        "department": "Traffic",
        "latitude": 23.0,
        "longitude": 72.5,
        "state": "degraded",
        "reason": "fps drift",
        "observed_fps": 4.0,
        "last_frame_at": datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
    }
    row.update(overrides)
    return row


async def test_cameras_geojson_builds_a_maplibre_feature_collection():
    pool = FakePool([_geo_row()])
    fc = await cameras_geojson(pool, scope="gj")
    assert fc["type"] == "FeatureCollection"
    [feature] = fc["features"]
    # GeoJSON is [lon, lat] — the ordering a flipped pair silently breaks.
    assert feature["geometry"] == {"type": "Point", "coordinates": [72.5, 23.0]}
    assert feature["properties"]["state"] == "degraded"
    assert feature["properties"]["last_frame_at"] == "2026-09-01T10:00:00+00:00"


async def test_cameras_geojson_adds_the_bbox_as_an_envelope_clause():
    pool = FakePool([])
    await cameras_geojson(pool, scope="gj", bbox=(72.0, 23.0, 73.0, 24.0), limit=100)
    sql, args = pool.queries[0]
    assert "ST_MakeEnvelope($2, $3, $4, $5, 4326)" in sql
    assert args == ("gj", 72.0, 23.0, 73.0, 24.0, 100)


async def test_cameras_geojson_without_bbox_and_with_null_frame_time():
    pool = FakePool([_geo_row(last_frame_at=None)])
    fc = await cameras_geojson(pool, scope="gj")
    assert "ST_MakeEnvelope" not in pool.queries[0][0]
    assert fc["features"][0]["properties"]["last_frame_at"] is None
