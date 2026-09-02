-- The camera registry — Reference Model 1's core entity.
--
-- IDENTITY INVARIANT (from camera.proto): `id` is ours, stable and internal.
-- The gateway's id, the department's asset tag and the vendor's serial are
-- attributes, not identity. Departments renumber and vendors get replaced; a
-- camera must survive both without losing its detection history.
--
-- So the catalogue id is `external_id`, scoped by `source` — the adapter that
-- supplied it. Two gateways may legitimately both call a camera "12".

CREATE TABLE IF NOT EXISTS cameras (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Provenance of the record itself.
    source              text NOT NULL,
    external_id         text NOT NULL,

    -- Where and whose.
    location            geography(Point, 4326),
    site_name           text,
    district            text,
    department          text,
    owner               text,

    -- What it is.
    camera_type         text NOT NULL DEFAULT 'unspecified',
    vendor              text,
    vms_platform        text,
    codec               text,
    native_width        integer,
    native_height       integer,

    -- The catalogue's stated frame rate. Recorded for reference and for drift
    -- reporting ONLY. Nothing derives time from it — the integrator's guide is
    -- explicit that the declared rate does not match delivery, and observed_fps
    -- below (measured from PTS) is the number that counts.
    declared_fps        real,

    -- How to reach it. Refreshed from the catalogue on every sync.
    -- NEVER construct these by string-formatting an id: the catalogue is the
    -- contract, the URL pattern is not.
    rtsp_url            text,
    hls_url             text,
    whep_url            text,

    -- Storage and lifecycle. Retention varies by department (7-15+ days) and an
    -- evidence request needs to know whether the footage still exists before an
    -- officer goes looking for it.
    storage_location    text,
    retention_days      integer,
    commissioned_at     timestamptz,
    amc_expires_at      timestamptz,

    -- Registry lifecycle, distinct from health.
    --   active          in service
    --   absent          was in the catalogue, no longer is. NOT deleted: the
    --                   detections it produced remain evidence and must keep a
    --                   camera to point at.
    --   decommissioned  an operator retired it. Survives re-appearance in the
    --                   catalogue, so a stale gateway entry cannot silently
    --                   resurrect a camera someone deliberately switched off.
    lifecycle               text NOT NULL DEFAULT 'active',
    catalogue_live          boolean NOT NULL DEFAULT true,
    present_in_catalogue    boolean NOT NULL DEFAULT false,
    last_seen_in_catalogue  timestamptz,

    -- Current health, denormalised from the heartbeat stream so the map query
    -- is one indexed scan rather than a per-camera time-series lookup.
    -- Authority still lives in camera_heartbeat; this is a cache of the last
    -- derivation.
    health_state            text NOT NULL DEFAULT 'unknown',
    health_reason           text,
    last_heartbeat_at       timestamptz,
    last_frame_at           timestamptz,
    observed_fps            real,
    black_frame_ratio       real,
    tamper_suspected        boolean NOT NULL DEFAULT false,
    consecutive_failures    integer NOT NULL DEFAULT 0,
    loop_epoch              integer NOT NULL DEFAULT 0,
    last_error              text,

    -- How long without a heartbeat before this camera is presumed unreachable.
    -- Per-camera because a 1 fps overview camera and a 25 fps ANPR camera do not
    -- deserve the same patience. Default is ~4x the worker heartbeat interval.
    stale_after_s       integer NOT NULL DEFAULT 45,

    -- The catalogue entry exactly as received. Field names are not yet pinned
    -- down (see prahari_common.catalogue); keeping the payload means a wrong
    -- guess about a key is recoverable with an UPDATE instead of a re-sync.
    raw                 jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT cameras_source_external_id_key UNIQUE (source, external_id),
    CONSTRAINT cameras_lifecycle_check
        CHECK (lifecycle IN ('active', 'absent', 'decommissioned')),
    CONSTRAINT cameras_health_state_check
        CHECK (health_state IN ('unknown', 'healthy', 'degraded', 'unreachable', 'tampered')),
    CONSTRAINT cameras_camera_type_check
        CHECK (camera_type IN ('unspecified', 'analog', 'ip', 'ptz', 'anpr'))
);

-- GIST for the spatial predicates gap analysis runs: nearest-neighbour (<->),
-- ST_DWithin, and bounding-box filters for the map viewport.
CREATE INDEX IF NOT EXISTS cameras_location_idx ON cameras USING gist (location);
CREATE INDEX IF NOT EXISTS cameras_district_idx ON cameras (district) WHERE lifecycle = 'active';
CREATE INDEX IF NOT EXISTS cameras_department_idx ON cameras (department) WHERE lifecycle = 'active';
CREATE INDEX IF NOT EXISTS cameras_health_idx ON cameras (health_state) WHERE lifecycle = 'active';
CREATE INDEX IF NOT EXISTS cameras_heartbeat_idx ON cameras (last_heartbeat_at);

-- Effective health, with the staleness overlay applied at READ time.
--
-- This is the one health rule that is not computed by the heartbeat handler,
-- and it is in SQL on purpose. A camera goes unreachable by heartbeats *not
-- arriving*, so no process is running to write that transition. Deriving it in
-- a background sweep would mean a killed registry pod leaves every camera
-- looking healthy until it is rescheduled; deriving it in the query means the
-- answer is correct the instant anyone asks, on any replica, with no timer.
CREATE OR REPLACE VIEW camera_current AS
SELECT
    c.*,
    CASE
        WHEN c.lifecycle <> 'active'    THEN 'unknown'
        WHEN c.last_heartbeat_at IS NULL THEN 'unknown'
        WHEN c.last_heartbeat_at < now() - make_interval(secs => c.stale_after_s)
            THEN 'unreachable'
        ELSE c.health_state
    END AS effective_health_state,
    CASE
        WHEN c.lifecycle <> 'active'    THEN 'not in service'
        WHEN c.last_heartbeat_at IS NULL THEN 'no heartbeat received yet'
        WHEN c.last_heartbeat_at < now() - make_interval(secs => c.stale_after_s)
            THEN 'no heartbeat for over ' || c.stale_after_s || 's'
        ELSE c.health_reason
    END AS effective_health_reason,
    ST_Y(c.location::geometry) AS latitude,
    ST_X(c.location::geometry) AS longitude
FROM cameras c;
