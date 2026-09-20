-- The correlation service's durable sighting ledger.
--
-- Lives in the SAME database as the registry's tables (the chart hands every
-- Postgres client the same DSN); the `correlation_` prefix is the ownership
-- boundary — registry migrations never touch this table, this service's
-- migrations never touch `cameras`/`camera_heartbeat`/etc. Version rows land
-- in the shared `schema_migration` ledger; the filename is unique across
-- services so versions cannot collide.
--
-- `detection_pb` is the serialized `prahari.v1.VehicleDetection` and is the
-- source of truth for reconstruction: route building needs fields that have
-- no dedicated column (appearance_embedding for gap bridging, pts_ms,
-- evidence_ref), and re-deriving them from a partial row would drift from
-- the bus contract. The named columns exist for indexing and ad-hoc
-- querying only — never reconstruct a detection from them.

CREATE TABLE IF NOT EXISTS correlation_sightings (
    detection_id      text PRIMARY KEY,
    -- detection.detection_id when the publisher set one; the store's
    -- fallback dedup key (camera_id:pts_ms:raw_text) otherwise. Either way
    -- it is stable across XADD retries and consumer-group redelivery, which
    -- is what makes ON CONFLICT DO NOTHING the whole idempotency story.
    camera_id         text        NOT NULL,
    plate_skeleton    text,        -- NULL when no plate was legible: the unplated pool
    plate_normalised  text,        -- evidence/display value, never queried for identity
    char_confidences  jsonb,       -- PlateReading.char_confidence, kept intact (CLAUDE.md)
    observed_at       timestamptz NOT NULL,
    stream_entry_id   text        NOT NULL,  -- the Redis stream id it was consumed under
    detection_pb      bytea       NOT NULL,  -- full VehicleDetection, source of truth
    inserted_at       timestamptz NOT NULL DEFAULT now()
);

-- Route reconstruction: "every sighting of this plate, in time order".
CREATE INDEX IF NOT EXISTS correlation_sightings_plate_idx
    ON correlation_sightings (plate_skeleton, observed_at);

-- "What did this camera see, when" — corridor/gap queries and debugging.
CREATE INDEX IF NOT EXISTS correlation_sightings_camera_idx
    ON correlation_sightings (camera_id, observed_at);

-- Appearance bridging scans only the plate-unreadable rows, in a bounded
-- time window. A partial index keeps that scan off the plated majority.
CREATE INDEX IF NOT EXISTS correlation_sightings_unplated_idx
    ON correlation_sightings (observed_at)
    WHERE plate_skeleton IS NULL;
