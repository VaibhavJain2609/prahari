-- Identity: users, sessions, API keys.
--
-- Owned by the registry's migration runner (services/registry/src/prahari_registry/db.py),
-- the same checksummed, advisory-locked runner every other migration uses — not because
-- identity belongs to the registry conceptually, but because users.org_id and api_keys.org_id
-- need referential integrity with orgs, which cameras also references. Splitting identity into
-- a second database would leave that FK nothing can enforce. See docs/ORG-TIERS-DESIGN.md §3.1.
--
-- The BFF (services/bff) is the only service that reads and writes these tables. It connects to
-- this same Postgres directly for identity, but never writes `cameras` — camera mutations always
-- go through the registry's own HTTP API, so validation and MediaMTX reconciliation stay on one
-- path.

CREATE TABLE users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username      text NOT NULL UNIQUE,
    password_hash text NOT NULL,       -- argon2id, via argon2-cffi
    org_id        uuid NOT NULL REFERENCES orgs(id),
    role          text NOT NULL CHECK (role IN ('viewer', 'operator', 'admin')),
    -- Role is held at this org node and applies to its whole subtree — checked
    -- against `orgs.path <@ <the org being acted on>` at the point of use, the
    -- same predicate every scoped camera read already applies. Not encoded here.
    created_at    timestamptz NOT NULL DEFAULT now(),
    disabled_at   timestamptz
    -- Sticky, like camera decommissioning: a disabled user's existing sessions
    -- are still rejected at resolve time (see SessionRepository.resolve), not
    -- just at future logins.
);

CREATE TABLE sessions (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- The session id IS the cookie value. A v4 UUID has 122 bits of randomness,
    -- which is enough entropy that no separate opaque token is needed — the
    -- primary key itself is unguessable.
    user_id    uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
);

CREATE INDEX sessions_user_idx ON sessions (user_id);

CREATE TABLE api_keys (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    key_hash      text NOT NULL UNIQUE,   -- sha256(plaintext); plaintext shown once, at creation
    org_id        uuid NOT NULL REFERENCES orgs(id),
    role          text NOT NULL CHECK (role IN ('viewer', 'operator', 'admin')),
    purpose       text NOT NULL CHECK (
        purpose IN ('local_body_registration', 'onvif_agent', 'vendor_adapter', 'internal_service')
    ),
    -- One table, one resolver, four purposes — the point of unifying every
    -- non-interactive credential behind the same Principal as a session.
    label         text NOT NULL,
    created_by    uuid REFERENCES users(id),
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_used_at  timestamptz,
    revoked_at    timestamptz
);

CREATE INDEX api_keys_org_idx ON api_keys (org_id) WHERE revoked_at IS NULL;
