"""Gap analysis — the part of Reference Model 1 that makes the registry more
than an inventory.

A list of 80,000 cameras answers "what do we own". Gap analysis answers the
questions an officer actually has:

* Which districts are running below the coverage they are credited with?
* This camera is down — does anything else see that junction, or is it a hole?
* Where is the nearest working camera to the incident I am standing at?

All three are spatial and all three depend on *live* health rather than
nameplate status, which is why they read `camera_current` and not `cameras`.

Every function below takes `scope` as a required, non-defaulted keyword — an
ltree path (`orgs.path`, migrations/005_orgs.sql) joined against
`camera_current.org_id`. A district-coverage query with no scope is a
statewide query with an org filter bolted on after the fact; requiring it
here is what keeps a local body's dark-zone view from ever being able to
answer for cameras it cannot see, the same discipline `repository.py`
applies to every other read.
"""

from __future__ import annotations

import asyncpg

from .models import DarkZone, DistrictCoverage, GeoPoint, HealthState, NearestCamera


async def district_coverage(pool: asyncpg.Pool, *, scope: str) -> list[DistrictCoverage]:
    """Per-district health census, within `scope`.

    `coverage_pct` is healthy-over-registered, deliberately counting degraded
    and tampered cameras as *not* coverage. A camera delivering 2 fps of a
    covered lens is on the asset register and is not watching anything.
    """
    rows = await pool.fetch(
        """
        SELECT
            c.district,
            count(*) FILTER (WHERE c.lifecycle = 'active')                       AS registered,
            count(*) FILTER (WHERE c.lifecycle = 'active'
                             AND c.effective_health_state = 'healthy')           AS healthy,
            count(*) FILTER (WHERE c.lifecycle = 'active'
                             AND c.effective_health_state = 'degraded')          AS degraded,
            count(*) FILTER (WHERE c.lifecycle = 'active'
                             AND c.effective_health_state = 'unreachable')       AS unreachable,
            count(*) FILTER (WHERE c.lifecycle = 'active'
                             AND c.effective_health_state = 'tampered')          AS tampered,
            count(*) FILTER (WHERE c.lifecycle = 'active'
                             AND c.effective_health_state = 'unknown')           AS unknown,
            count(*) FILTER (WHERE c.lifecycle = 'absent')                       AS absent
        FROM camera_current c
        JOIN orgs o ON o.id = c.org_id
        WHERE o.path <@ $1::ltree
        GROUP BY c.district
        ORDER BY registered DESC, c.district NULLS LAST
        """,
        scope,
    )
    out: list[DistrictCoverage] = []
    for r in rows:
        registered = r["registered"]
        out.append(
            DistrictCoverage(
                district=r["district"],
                registered=registered,
                healthy=r["healthy"],
                degraded=r["degraded"],
                unreachable=r["unreachable"],
                tampered=r["tampered"],
                unknown=r["unknown"],
                absent=r["absent"],
                coverage_pct=round(100.0 * r["healthy"] / registered, 1) if registered else 0.0,
            )
        )
    return out


async def dark_zones(pool: asyncpg.Pool, *, scope: str, radius_m: float) -> list[DarkZone]:
    """Cameras in `scope` that are down with no healthy camera within
    `radius_m`.

    The LATERAL subquery uses the KNN operator (`<->`) so PostGIS walks the
    GIST index outward from each camera and stops at the first healthy
    neighbour, instead of computing every pairwise distance. At 80,000
    cameras the difference is between a query and an outage.

    The "is anything else covering this junction" neighbour search is
    deliberately **not** scoped — a down camera at a local body's own
    junction is still covered if the org next door has a working camera
    fifty metres away, and a scope-blind dark-zone report would tell an
    operator to fund a redundant camera the estate already has. Scope narrows
    which cameras are *candidates for being reported down*, never which
    cameras count as covering for them.
    """
    rows = await pool.fetch(
        """
        SELECT
            c.id, c.site_name, c.district, c.latitude, c.longitude,
            c.effective_health_state AS state, c.effective_health_reason AS reason,
            n.distance_m
        FROM camera_current c
        JOIN orgs o ON o.id = c.org_id
        LEFT JOIN LATERAL (
            SELECT ST_Distance(c.location, h.location) AS distance_m
            FROM camera_current h
            WHERE h.effective_health_state = 'healthy'
              AND h.location IS NOT NULL
              AND h.id <> c.id
            ORDER BY c.location <-> h.location
            LIMIT 1
        ) n ON true
        WHERE o.path <@ $1::ltree
          AND c.lifecycle = 'active'
          AND c.location IS NOT NULL
          AND c.effective_health_state <> 'healthy'
          AND (n.distance_m IS NULL OR n.distance_m > $2)
        ORDER BY n.distance_m DESC NULLS FIRST
        LIMIT 500
        """,
        scope,
        radius_m,
    )
    return [
        DarkZone(
            camera_id=str(r["id"]),
            site_name=r["site_name"],
            district=r["district"],
            location=GeoPoint(latitude=r["latitude"], longitude=r["longitude"]),
            state=HealthState(r["state"]),
            reason=r["reason"],
            nearest_healthy_m=round(r["distance_m"], 1) if r["distance_m"] is not None else None,
        )
        for r in rows
    ]


async def nearest_cameras(
    pool: asyncpg.Pool,
    *,
    scope: str,
    latitude: float,
    longitude: float,
    limit: int = 5,
    healthy_only: bool = True,
) -> list[NearestCamera]:
    """Cameras in `scope` nearest a point, closest first.

    `healthy_only` defaults to true because the question being asked is almost
    always "what can I actually look at", not "what is on the map here".
    Distances are metres: the column is `geography`, so ST_Distance is on the
    spheroid and needs no projection chosen per district.
    """
    rows = await pool.fetch(
        f"""
        SELECT
            c.id, c.site_name, c.district, c.latitude, c.longitude,
            c.effective_health_state AS state,
            ST_Distance(c.location, ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography) AS distance_m
        FROM camera_current c
        JOIN orgs o ON o.id = c.org_id
        WHERE o.path <@ $1::ltree AND c.lifecycle = 'active' AND c.location IS NOT NULL
          {"AND c.effective_health_state = 'healthy'" if healthy_only else ""}
        ORDER BY c.location <-> ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography
        LIMIT $4
        """,
        scope,
        longitude,
        latitude,
        limit,
    )
    return [
        NearestCamera(
            camera_id=str(r["id"]),
            site_name=r["site_name"],
            district=r["district"],
            location=GeoPoint(latitude=r["latitude"], longitude=r["longitude"]),
            state=HealthState(r["state"]),
            distance_m=round(r["distance_m"], 1),
        )
        for r in rows
    ]


async def cameras_geojson(
    pool: asyncpg.Pool,
    *,
    scope: str,
    bbox: tuple[float, float, float, float] | None = None,
    limit: int = 20_000,
) -> dict:
    """A FeatureCollection MapLibre can consume directly, restricted to
    `scope`.

    Built server-side so the console does not re-derive health from raw
    columns and drift from what the API says. Properties are kept to what the
    map styles on; full detail comes from /cameras/{id} when a pin is
    clicked. `stream_secret` is never selected here or anywhere in this
    module — see the credential-handling note in models.py.
    """
    args: list[object] = [scope]
    where = ["o.path <@ $1::ltree", "c.lifecycle = 'active'", "c.location IS NOT NULL"]
    if bbox is not None:
        args.extend(bbox)
        i = len(args)
        where.append(
            f"c.location::geometry && ST_MakeEnvelope(${i - 3}, ${i - 2}, ${i - 1}, ${i}, 4326)"
        )
    args.append(limit)

    rows = await pool.fetch(
        f"""
        SELECT c.id, c.site_name, c.district, c.department, c.latitude, c.longitude,
               c.effective_health_state AS state, c.effective_health_reason AS reason,
               c.observed_fps, c.last_frame_at
        FROM camera_current c
        JOIN orgs o ON o.id = c.org_id
        WHERE {" AND ".join(where)}
        LIMIT ${len(args)}
        """,
        *args,
    )
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": str(r["id"]),
                "geometry": {"type": "Point", "coordinates": [r["longitude"], r["latitude"]]},
                "properties": {
                    "id": str(r["id"]),
                    "site_name": r["site_name"],
                    "district": r["district"],
                    "department": r["department"],
                    "state": r["state"],
                    "reason": r["reason"],
                    "observed_fps": r["observed_fps"],
                    "last_frame_at": r["last_frame_at"].isoformat() if r["last_frame_at"] else None,
                },
            }
            for r in rows
        ],
    }
