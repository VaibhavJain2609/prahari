-- Inference-worker membership: the registry's answer to "who is pulling, and
-- which slice of the estate each one owns".
--
-- Before this table every worker ran the same `GET /api/v1/cameras` and sliced
-- the same first-N cameras off an identical list — N pods racing the same
-- cameras, and a KEDA scale event pulling the whole catalogue per pod. Workers
-- now POST /api/v1/workers/register (idempotent, refreshes last_seen) and read
-- their slice from /api/v1/assignments, which shards the active camera set by
-- (row_number-1) % shard_count over a stable ordering.
--
-- Membership is a lease, not a lock: a worker counts as alive while
-- last_seen is within 2x assignment_lease_s, and the row itself is reaped once
-- last_seen is 3x the lease stale. No DELETE endpoint exists — a dead pod's
-- lease simply expires, which is the only teardown a crashed container can
-- be relied on to perform.

CREATE TABLE workers (
    worker_id     text PRIMARY KEY,        -- pod name / hostname, caller-supplied
    registered_at timestamptz NOT NULL DEFAULT now(),
    last_seen     timestamptz NOT NULL DEFAULT now(),

    -- The last (index, count) handed out at register time, persisted so the
    -- table alone can answer "which shard did worker X last believe it owned"
    -- without replaying the membership query. Stale rows keep stale values —
    -- the alive predicate, not these columns, decides who counts.
    shard_index   integer NOT NULL DEFAULT 0,
    shard_count   integer NOT NULL DEFAULT 1
);

CREATE INDEX workers_last_seen_idx ON workers (last_seen);
