-- Audited evidence-clip requests — the durable record behind the only
-- sanctioned central pull of video (docs/EVIDENCE.md).
--
-- Video stays at the edge; what lands centrally is this row: who asked, for
-- which camera, over what window, under which purpose code, and which ticket
-- was minted for it. The BFF (services/bff) is the only service that reads
-- and writes this table — the same pattern as 006_identity.sql: the table
-- lives in the registry's checksummed, advisory-locked migration runner
-- because that runner owns every table in the shared database, and because
-- camera_id wants a real foreign key to cameras(id). The registry's own
-- repository never touches it.
--
-- Status is deliberately thin: `pending` (stored and audited, no ticket yet)
-- and `issued` (a playback ticket was minted — ticket_jti/ticket_expires_at
-- record which). There is no `expired` transition to write: expiry is a
-- property of the minted ticket's `exp` claim, derived at read time, not a
-- row update some sweeper would have to remember to make.

CREATE TABLE evidence_requests (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_id         uuid NOT NULL REFERENCES cameras(id),

    -- The camera's org path AT REQUEST TIME, denormalised so that admin
    -- listing scopes by `org_path <@ caller_scope` without joining
    -- cameras → orgs, and so a later org reassignment cannot rewrite the
    -- record's answer to "whose subtree was this camera in when the footage
    -- was asked for".
    org_path          ltree NOT NULL,

    requested_by      text NOT NULL,        -- the Principal.subject who asked
    purpose_code      text NOT NULL,        -- caller-chosen, mirrored into the audit row
    start_ts          timestamptz NOT NULL,
    end_ts            timestamptz NOT NULL,

    -- The edge-side clip locator this request records:
    -- `dvr://<camera_id>/<start_epoch>-<end_epoch>`. It is not a URL anything
    -- central resolves — fulfilment is an operator-side pull from the
    -- DVR/gateway (see docs/EVIDENCE.md), and this string is what that
    -- process and the audit trail both name.
    evidence_ref      text NOT NULL,

    status            text NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'issued')),

    -- Which ticket was minted, for forensics ("was THIS jwt the one we
    -- issued?"): the `jti` claim and its expiry — never the token itself,
    -- which is a bearer credential and does not belong at rest here.
    ticket_jti        text,
    ticket_expires_at timestamptz,

    created_at        timestamptz NOT NULL DEFAULT now(),
    issued_at         timestamptz,

    CONSTRAINT evidence_requests_range_check CHECK (end_ts > start_ts)
);

-- Admin listing is a scoped subtree scan; camera lookup is the audit trail's
-- "every request ever made against this camera" question.
CREATE INDEX evidence_requests_org_idx ON evidence_requests (org_path);
CREATE INDEX evidence_requests_camera_idx ON evidence_requests (camera_id, created_at);
