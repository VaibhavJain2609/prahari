-- Org hierarchy — arbitrary-depth tree behind the three-board split (global /
-- organization / local body) over one shared camera estate. See
-- docs/ORG-TIERS-DESIGN.md.
--
-- ltree gives "state -> district -> city -> zone" (or any other depth) one
-- predicate: `path <@ $scope`. A level appearing later needs no schema change
-- and no query rewrite.

CREATE EXTENSION IF NOT EXISTS ltree;

CREATE TABLE orgs (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    parent_id  uuid REFERENCES orgs(id),
    path       ltree NOT NULL UNIQUE,   -- e.g. gj.ahmedabad_city.zone_4
    kind       text NOT NULL,           -- state | organization | local_body — a label, not structure
    name       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT orgs_kind_check CHECK (kind IN ('state', 'organization', 'local_body'))
);
CREATE INDEX orgs_path_gist ON orgs USING gist (path);
CREATE INDEX orgs_parent_idx ON orgs (parent_id);

-- Seed the state root. sync_default_org_path (config.py) points at it, and a
-- fresh cluster needs somewhere for both the catalogue sync and manual
-- registration to land without a bootstrap step of their own.
INSERT INTO orgs (path, kind, name) VALUES ('gj', 'state', 'Gujarat');

-- Cameras join the tree.
ALTER TABLE cameras ADD COLUMN org_id  uuid REFERENCES orgs(id);
ALTER TABLE cameras ADD COLUMN adapter text NOT NULL DEFAULT 'manual';
ALTER TABLE cameras ADD CONSTRAINT cameras_adapter_check
    CHECK (adapter IN ('gateway', 'rtsp-direct', 'onvif', 'manual'));

-- Every camera that existed before this migration is backfilled onto the
-- state root rather than left with a NULL org_id. A camera org_id cannot
-- scope query cannot see would not merely be "unscoped" — with the join in
-- repository.py it would become invisible and unreachable from every board,
-- global included. NOT NULL below then makes "every camera has an org" a
-- schema fact, not a convention a future INSERT can forget.
UPDATE cameras SET org_id = (SELECT id FROM orgs WHERE path = 'gj') WHERE org_id IS NULL;
ALTER TABLE cameras ALTER COLUMN org_id SET NOT NULL;

CREATE INDEX cameras_org_idx ON cameras (org_id) WHERE lifecycle = 'active';

-- Credentials for locally-registered analog/DVR cameras (Stage 4 wires the
-- AES-GCM encrypt/decrypt path; the columns land now because they are part of
-- the same ALTER TABLE pass). Never selected into the Camera API model — see
-- repository.py's camera_from_row, which maps named columns rather than `*`.
ALTER TABLE cameras ADD COLUMN stream_username text;
ALTER TABLE cameras ADD COLUMN stream_secret   bytea;

-- camera_current is `SELECT c.*` (002_cameras.sql) plus derived columns, so
-- Postgres froze its column list at creation. The new cameras columns above
-- land before the derived ones in `c.*`, and CREATE OR REPLACE VIEW cannot
-- reorder view columns — it fails with "cannot change name of view column".
-- So: drop and recreate, body otherwise identical to migration 002.
DROP VIEW camera_current;
CREATE VIEW camera_current AS
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
