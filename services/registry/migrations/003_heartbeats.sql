-- Camera health as a time-series.
--
-- The denormalised columns on `cameras` answer "is it up now". This table
-- answers "was it up at 14:20 on the 3rd", which is the question a coverage
-- dispute or an evidence challenge actually asks. It is also the baseline for
-- FPS drift: a camera is degraded relative to ITS OWN normal delivery rate, not
-- relative to the rate the catalogue claims for it.

CREATE TABLE IF NOT EXISTS camera_heartbeat (
    camera_id           uuid NOT NULL REFERENCES cameras (id) ON DELETE CASCADE,
    observed_at         timestamptz NOT NULL,

    worker_id           text NOT NULL,
    connected           boolean NOT NULL,

    -- Measured from PTS deltas (PTSClock.measured_fps), never from
    -- CAP_PROP_FPS. Null until the worker has seen enough frames to measure.
    measured_fps        real,

    last_frame_at       timestamptz,
    frames_decoded      bigint NOT NULL DEFAULT 0,
    consecutive_failures integer NOT NULL DEFAULT 0,
    black_frame_ratio   real,

    -- The worker's raw suspicion, not a verdict. Feeds loop and cut scene
    -- abruptly on every cycle; the registry requires the flag to persist across
    -- several heartbeats before it will call a camera tampered.
    tamper_suspected    boolean NOT NULL DEFAULT false,

    -- Increments on every discontinuity the worker sees. A jump here across
    -- consecutive heartbeats is a loop wrap or a reconnect — expected, and the
    -- reason a single black frame or scene cut is never an alert on its own.
    loop_epoch          integer NOT NULL DEFAULT 0,

    last_error          text
);

-- Timescale requires the partitioning column in any unique index, which is why
-- there is no surrogate primary key here.
CREATE INDEX IF NOT EXISTS camera_heartbeat_camera_time_idx
    ON camera_heartbeat (camera_id, observed_at DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        PERFORM create_hypertable(
            'camera_heartbeat', 'observed_at',
            chunk_time_interval => INTERVAL '1 day',
            if_not_exists => TRUE
        );
        -- 80,000 cameras at one heartbeat every 10s is ~690M rows/day. Health
        -- history older than a fortnight has no operational use and is not
        -- evidence, so it is dropped rather than archived.
        PERFORM add_retention_policy('camera_heartbeat', INTERVAL '14 days', if_not_exists => TRUE);
    ELSE
        RAISE WARNING
            'camera_heartbeat created as a plain table: no automatic retention. '
            'Delete rows older than 14 days externally, or this grows without bound.';
    END IF;
END
$$;
