-- Alert history: the durable record behind GET /api/v1/alerts.
--
-- The Redis stream (`prahari:alerts`) is the *live relay* — MAXLEN-bounded,
-- consumed by the BFF for SSE, and allowed to drop old entries. This table is
-- the *system of record*: every alert the engine emits is inserted here before
-- the stream publish is attempted, so history survives restarts and stream
-- trimming. Columns mirror the `Alert` proto one-to-one where a query needs
-- them (plate, camera_id, occurred_at, confidence); the full message also
-- lands in `payload` so readers get back the exact alert the bus saw.

CREATE TABLE alerts (
    id              bigserial PRIMARY KEY,

    alert_id        text        NOT NULL UNIQUE,  -- Alert.alert_id (uuid4)
    dedup_key       text        NOT NULL,         -- (camera, plate, time-bucket)

    -- Denormalised query columns. `plate` is the *observed* plate
    -- (MatchExplanation.observed_plate) — what OCR actually read on camera —
    -- because that is what an officer types into a search box. The matched
    -- watchlist plate stays reachable via explanation->>'matched_plate'.
    plate           text        NOT NULL,
    camera_id       text        NOT NULL,         -- Alert.detection.camera_id

    confidence      double precision NOT NULL,    -- MatchExplanation.final_score
    priority        smallint    NOT NULL,         -- AlertPriority enum number
    band            smallint    NOT NULL,         -- ConfidenceBand enum number

    explanation     jsonb       NOT NULL,         -- MatchExplanation as JSON
    payload         jsonb       NOT NULL,         -- whole Alert, MessageToDict

    -- When the vehicle was seen: detection.observed_at.wall_clock, falling
    -- back to alert.raised_at when the worker sent no wall clock.
    occurred_at     timestamptz NOT NULL,

    -- Acknowledge-only lifecycle (audit decision: no assignment workflow).
    -- NULL means unacknowledged; the pair is written together, first write wins.
    acknowledged_at timestamptz,
    acknowledged_by text,

    created_at      timestamptz NOT NULL DEFAULT now()
);

-- The two access patterns the list endpoint is built around: "what did this
-- camera see, newest first" and "where was this plate seen".
CREATE INDEX alerts_camera_occurred_idx ON alerts (camera_id, occurred_at DESC);
CREATE INDEX alerts_plate_idx ON alerts (plate);

-- The console's default view is the unacknowledged queue; a partial index
-- keeps that scan off the full table once history grows.
CREATE INDEX alerts_unacknowledged_idx ON alerts (occurred_at DESC)
    WHERE acknowledged_at IS NULL;
