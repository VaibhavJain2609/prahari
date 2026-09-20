# Evidence pulls — the audited way video leaves the edge

The platform's one idea is that pixels stay at the edge. This document is the
honest account of the *only* sanctioned exception: an explicit, scoped,
hash-chain-audited evidence request. It exists so the privacy invariant —
"every video access is written to the audit log, with an actor and a purpose
code" — covers recorded footage and not just the live preview path.

## What exists today, and what does not

**What exists:** the request chain itself — create, store, audit, scope,
issue a playback-scoped MediaMTX ticket, list. Plus the `evidence_requests`
table (migration `008_evidence_requests.sql`), so the request is a durable
row rather than a log line.

**What does not exist:** central video bytes. MediaMTX recording is **off** —
the paths the registry reconciles (`prahari_registry.mediamtx._path_config`)
set `source`, `sourceOnDemand*` and `rtspTransport`, and no `record` key.
Nothing here turns it on. Consequently a playback ticket today is a valid,
verifiable credential that MediaMTX has nothing to serve against: the auth
callback will authorise `playback` on `cam-<id>`, and the restreamer will
then have no recording to play. That is deliberate — the ticket is designed
so that `record: yes` (plus `recordPath`/`recordDeleteAfter`) landing on the
reconciled paths later changes **nothing** in the request/ticket/audit
chain.

**Fulfilment today is an edge-side process.** The `evidence_ref` a request
records — `dvr://<camera_id>/<start_epoch>-<end_epoch>` — is a locator, not
a URL anything central resolves. Pulling the named window off the DVR or
government gateway is operator/edge work; this system records *who asked,
for which camera, which window, under which purpose code, and which ticket
was minted*. That audit trail is the deliverable of v1.

## The request chain

```
POST /api/v1/evidence/requests          {camera_id, start_ts, end_ts, purpose_code}
GET  /api/v1/evidence/requests/{id}     state of one request   (X-Purpose-Code)
POST /api/v1/evidence/requests/{id}/ticket   mint playback ticket (X-Purpose-Code)
GET  /api/v1/evidence/requests          admin list, scoped     (admin role)
```

All on the BFF. `purpose_code` rides in the create *body* (not the
`X-Purpose-Code` header) because it is part of the durable record — stored
on the row and mirrored into the audit entry. The read and ticket endpoints
take the header like every other evidence-adjacent read.

### Create — `POST /api/v1/evidence/requests` → 201

Ordering, in the order the code does it:

1. **Window validation.** Both timestamps must be timezone-aware; `end_ts >
   start_ts`; the span must be ≤ `PRAHARI_EVIDENCE_MAX_RANGE_S` (default
   900 s / 15 min). The bound exists because the request is a scoped grant
   on someone's footage — an unbounded window would let one purpose code
   justify an arbitrary sweep.
2. **Scope check.** The camera's org is resolved root-scoped
   (`CameraScopeResolver`) and compared against the caller's subtree.
   Unknown camera → 404 (nothing accessed, nothing audited). Out of scope →
   `evidence_request_denied` audit row, then 403 — a cross-org attempt is
   itself an audit event, not a silent drop.
3. **Audit before insert.** `evidence_requested` is appended to the
   hash-chained log *before* the row exists, failing closed (500) if the
   append fails. A stored request with no audit row is the exact failure
   this ordering prevents.
4. **Insert** `pending` row with the `dvr://` locator.

### Read — `GET /api/v1/evidence/requests/{id}`

Scoped to the `org_path` recorded on the row (the camera's org *at request
time* — a later reassignment cannot move the request out of the subtree it
was asked under). Audited `evidence_read`, or `evidence_read_denied` + 403.

### Ticket — `POST /api/v1/evidence/requests/{id}/ticket`

Re-checks scope against the recorded org (`evidence_issue_denied` + 403
across the boundary), appends `evidence_issued` **before** the credential
exists — the same fail-closed ordering as `preview-ticket` — then mints an
Ed25519 JWT via `MediaTicketIssuer.mint(..., actions=("read", "playback"))`:

```json
"mediamtx_permissions": [
  {"action": "read",     "path": "cam-<id>"},
  {"action": "playback", "path": "cam-<id>"}
]
```

TTL is `PRAHARI_EVIDENCE_TICKET_TTL_S` (default 300 s — longer than the 60 s
preview ticket because the consumer is a review workflow, not a live
viewer). The row's `ticket_jti` and `ticket_expires_at` record *which*
credential was issued — never the token itself, which is a bearer
credential and does not belong at rest. `issued_at` keeps the first
issuance; re-minting is allowed and every mint is its own audit row.

### List — `GET /api/v1/evidence/requests`

Admin-only, scoped to the caller's subtree (`org_path <@ caller`), audited
`evidence_list` under the `admin` purpose code — a listing of "who asked
for whose footage" is itself evidence-adjacent and cannot be the one
unrecorded access on this path.

## Status lifecycle

`pending` → `issued` → `expired`. Only the first two are stored; `expired`
is **derived at read time** (`issued` + `ticket_expires_at ≤ now`). Nothing
runs when a ticket ages out, so no row transition exists for it — deriving
keeps the answer honest on every replica with no sweeper.

## media_auth change (registry side)

`authorize` previously refused `publish` **and** `playback` outright. Now
only `publish` is refused outright; `playback` is allowed through the
ticket branch: a request for action `playback` on `cam-*` carrying a `jwt`
is authorised iff the ticket has an exact `{"action": "playback", "path"}`
grant (`_ticket_grants` matches the action exactly — a read-only preview
ticket cannot play back). Internal credentials still cannot claim playback:
`internal:` gets api/metrics/pprof, `worker:` gets `read` on `cam-*`, and
neither falls through to the ticket check.

## Audit vocabulary

| Action                    | When                                            |
|---------------------------|-------------------------------------------------|
| `evidence_requested`      | request accepted, written before the row exists |
| `evidence_request_denied` | create attempted out-of-scope                   |
| `evidence_read`           | scoped read of one request                      |
| `evidence_read_denied`    | read attempted out-of-scope                     |
| `evidence_issued`         | playback ticket minted (before minting)         |
| `evidence_issue_denied`   | ticket mint attempted out-of-scope              |
| `evidence_list`           | admin subtree listing                           |

## Deliberately deferred

- **Real clip bytes.** `record: yes` (+ `recordPath`, `recordDeleteAfter`)
  on the reconciled MediaMTX paths is the follow-on. The ticket already
  grants `playback`, so the change is confined to `mediamtx.py` /
  `infra/helm` values — nothing in this chain moves.
- **`VehicleDetection.evidence_ref`.** The proto field
  (`events.proto:28`) exists and flows through correlation's route hops,
  but nothing populates it — `prahari_inference`'s `to_protobuf`
  (`detect/pipeline.py`) builds detections without it, and stamping it
  (camera + `observed_at` → `prahari://evidence/<camera>/<ts>`) is a change
  to the inference/correlation services, outside this branch's file set.
  The request-level `evidence_ref` on `evidence_requests` is populated;
  per-detection population is a separate, small piece of work.
- **Ticket revocation.** `ticket_jti` is recorded so a denylist *could* be
  checked at the auth callback later; none is implemented — the 300 s TTL
  is the containment, same posture as the 60 s preview ticket.
- **Edge-side fulfilment automation.** The `dvr://` locator assumes a
  human/edge pull today. A signed fetch URL against the DVR or a per-camera
  `record`-on-demand path are both later, separately reviewed work.
