"""Gap analysis — the part of Reference Model 1 that makes the registry more
than an inventory.

A list of 80,000 cameras answers "what do we own". Gap analysis answers the
questions an officer actually has:

* Which districts are running below the coverage they are credited with?
* This camera is down — does anything else see that junction, or is it a hole?
* Where is the nearest working camera to the incident I am standing at?

All three are spatial and all three depend on *live* health rather than
nameplate status, which is why they read `camera_current` and not `cameras`.
"""

from __future__ import annotations

import asyncpg

from .models import DarkZone, DistrictCoverage, GeoPoint, HealthState, NearestCamera


async def district_coverage(pool: asyncpg.Pool) -> list[DistrictCoverage]:
    """Per-district health census.

    `coverage_pct` is healthy-over-registered, deliberately counting degraded
    and tampered cameras as *not* coverage. A camera delivering 2 fps of a
    covered lens is on the asset register and is not watching anything.
    """
    rows = await pool.fetch(
        """
        SELECT
            district,
            count(*) FILTER (WHERE lifecycle = 'active')                       AS registered,
            count(*) FILTER (WHERE lifecycle = 'active'
                             AND effective_health_state = 'healthy')           AS healthy,
            count(*) FILTER (WHERE lifecycle = 'active'
                             AND effective_health_state = 'degraded')          AS degraded,
            count(*) FILTER (WHERE lifecycle = 'active'
                             AND effective_health_state = 'unreachable')       AS unreachable,
            count(*) FILTER (WHERE lifecycle = 'active'
                             AND effective_health_state = 'tampered')          AS tampered,
            count(*) FILTER (WHERE lifecycle = 'active'
                             AND effective_health_state = 'unknown')           AS unknown,
            count(*) FILTER (WHERE lifecycle = 'absent')                       AS absent
        FROM camera_current
        GROUP BY district
        ORDER BY registered DESC, district NULLS LAST
        """
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


async def dark_zones(pool: asyncpg.Pool, *, radius_m: float) -> list[DarkZone]:
    """Cameras that are down with no healthy camera within `radius_m`.

    The LATERAL subquery uses the KNN operator (`<->`) so PostGIS walks the GIST
    index outward from each camera and stops at the first healthy neighbour,
    instead of computing every pairwise distance. At 80,000 cameras the
    difference is between a query and an outage.

    Ordered worst-first: a blind spot with nothing for kilometres outranks one
    where the next camera is just over the threshold.
    """
    rows = await pool.fetch(
        """
        SELECT
            c.id, c.site_name, c.district, c.latitude, c.longitude,
            c.effective_health_state AS state, c.effective_health_reason AS reason,
            n.distance_m
        FROM camera_current c
        LEFT JOIN LATERAL (
            SELECT ST_Distance(c.location, h.location) AS distance_m
            FROM camera_current h
            WHERE h.effective_health_state = 'healthy'
              AND h.location IS NOT NULL
              AND h.id <> c.id
            ORDER BY c.location <-> h.location
            LIMIT 1
        ) n ON true
        WHERE c.lifecycle = 'active'
          AND c.location IS NOT NULL
          AND c.effective_health_state <> 'healthy'
          AND (n.distance_m IS NULL OR n.distance_m > $1)
        ORDER BY n.distance_m DESC NULLS FIRST
        LIMIT 500
        """,
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
    latitude: float,
    longitude: float,
    limit: int = 5,
    healthy_only: bool = True,
) -> list[NearestCamera]:
    """Cameras nearest a point, closest first.

    `healthy_only` defaults to true because the question being asked is almost
    always "what can I actually look at", not "what is on the map here".
    Distances are metres: the column is `geography`, so ST_Distance is on the
    spheroid and needs no projection chosen per district.
    """
    rows = await pool.fetch(
        f"""
        SELECT
            id, site_name, district, latitude, longitude,
            effective_health_state AS state,
            ST_Distance(location, ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography) AS distance_m
        FROM camera_current
        WHERE lifecycle = 'active' AND location IS NOT NULL
          {"AND effective_health_state = 'healthy'" if healthy_only else ""}
        ORDER BY location <-> ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
        LIMIT $3
        """,
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
    bbox: tuple[float, float, float, float] | None = None,
    limit: int = 20_000,
) -> dict:
    """A FeatureCollection MapLibre can consume directly.

    Built server-side so the console does not re-derive health from raw columns
    and drift from what the API says. Properties are kept to what the map styles
    on; full detail comes from /cameras/{id} when a pin is clicked.
    """
    args: list[object] = []
    where = ["lifecycle = 'active'", "location IS NOT NULL"]
    if bbox is not None:
        args.extend(bbox)
        where.append("location::geometry && ST_MakeEnvelope($1, $2, $3, $4, 4326)")
    args.append(limit)

    rows = await pool.fetch(
        f"""
        SELECT id, site_name, district, department, latitude, longitude,
               effective_health_state AS state, effective_health_reason AS reason,
               observed_fps, last_frame_at
        FROM camera_current
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
