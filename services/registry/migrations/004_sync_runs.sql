-- Catalogue sync audit.
--
-- Sync is the "zero-code onboarding" demo beat, so it needs to be inspectable
-- afterwards rather than only observable while it scrolls past in a log. It is
-- also the record that shows ids rotating between build day and judging day,
-- which is the concrete evidence for why nothing hardcodes a stream URL.

CREATE TABLE IF NOT EXISTS catalogue_sync_run (
    id              bigserial PRIMARY KEY,
    source          text NOT NULL,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    ok              boolean,

    cameras_seen    integer NOT NULL DEFAULT 0,
    cameras_added   integer NOT NULL DEFAULT 0,
    cameras_updated integer NOT NULL DEFAULT 0,
    cameras_absent  integer NOT NULL DEFAULT 0,

    -- Codec histogram at this point in time. §4 of the integrator's guide
    -- requires the pipeline handle mixed H.264/H.265; this records which mix we
    -- have actually been tested against, per sync.
    codec_mix       jsonb NOT NULL DEFAULT '{}'::jsonb,

    error           text
);

CREATE INDEX IF NOT EXISTS catalogue_sync_run_started_idx
    ON catalogue_sync_run (started_at DESC);
