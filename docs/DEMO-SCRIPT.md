# Demo script

The narrative for a live run-through: setup, then beats. Every beat names
what to click, what it proves, and the invariant it demonstrates. Nothing
below requires a feature that does not exist — where the honest answer is
"not built yet", the beat says so rather than routing around it.

## Setup

```bash
make cluster          # k3d (skippable if the cluster exists)
make proto images     # stubs first — images ImportError without them
# Secrets BEFORE up: secretKeyRef env binds at pod creation — a Secret
# created after the deploy is invisible until a restart.
make internal-secret  # internal-token + credential-key (create-if-absent)
make gateway-secret   # .env → prahari-gateway (catalogue sync needs it)
make bff-bootstrap    # first-login admin Secret (needs the two env vars in .env)
make up               # helm upgrade --install, profile=local
```

Console: `http://localhost:3000` (k3d port map). BFF: `:8080`. Sign in with
the bootstrap admin. Without `prahari-gateway`, catalogue sync degrades but
everything else runs — register cameras by hand on `/cameras` instead.

## Beat 1 — the map is live, not a spreadsheet

**Do:** land on `/` after sync has run (it runs on startup, then every 300 s).
The estate renders as a health-coloured MapLibre layer; the summary strip
counts cameras by health state. Click a camera → the drawer shows
`measured_fps`, last error, health history.

**Proves:** workers report observations; the registry computes staleness at
read time in `camera_current`. A camera that stops heartbeating goes stale on
the map with no code running to "notice" it.

**Invariant:** *workers observe; the registry decides.* Two workers on one
camera can never publish contradictory verdicts because no worker computes
one.

## Beat 2 — live preview is an audited act

**Do:** in the camera drawer set a purpose (`investigation` + a case ref) and
click "Live preview (audited)". The browser POSTs
`/api/v1/media/preview-ticket`, gets a scoped Ed25519 JWT, and opens the
MediaMTX-hosted WHEP player in a new tab with the ticket attached.

**Proves:** the audit write (`video_preview`) lands *before* the ticket is
minted — a failed audit write means no ticket, fail-closed. The JWT reads
only `cam-<id>` and expires in about a minute. `endpoints` is stripped from
every camera payload the browser sees; the ticket is the only path to pixels.

**Invariant:** *video never leaves the edge except as an explicit, audited
request.* The browser never holds a feed URL or a credential.

## Beat 3 — the mandatory case: plate → route

**Do:** in the trace dock, enter a registration number with purpose
`plate-trace`, run it. The route draws on the map — ordered hops with
timestamps — and any hop that failed the feasibility gate is listed as
rejected, not plotted. Export CSV/PDF from the same dock.

**Proves:** `GET /api/v1/routes/{plate}` stitches `prahari:detections`
sightings through correlation, gating each hop haversine-distance vs a speed
envelope. OCR confusions (`0/O/D`, `8/B`…) are priced by the match engine's
confusion matrix — an imperfect read still matches; an impossible hop still
doesn't.

**Invariant:** *fuzzy match correctness + feasibility gating.* Exact matching
would miss a real vehicle silently; ungated stitching would invent a route.
Both failure modes are visible in this one screen, so both are testable live.

## Beat 4 — alerts carry their justification

**Do:** trigger a watchlist plate (the loadtest harness can inject one:
`make loadtest LTARGS="run --cameras 5 --duration-s 60"`, or a live stream).
The alert lands on the console rail over SSE within seconds. Expand it — the
`MatchExplanation` shows observed plate, matched watchlist entry, per-
substitution cost, final score.

**Proves:** one alert per `(camera, plate, time-bucket)` — a vehicle in frame
8 s is one alert, not 24. History is the Postgres `alerts` table, queryable
via `GET /api/v1/alerts` with `?plate=`/`?camera_id=`/`?acknowledged=`;
`POST /api/v1/alerts/{id}/ack` is the only lifecycle transition, and it is
audited. Note honestly: the `/alerts` page is still a stub — the rail and the
API are real, the history screen is not wired yet.

**Invariant:** *every alert carries its justification* — explainable in
court, not a bare plate string.

## Beat 5 — org scoping is structural

**Do:** `/admin` → create an org under `gj` (e.g. `gj.demo_district`), a user
scoped to it. Open a second browser, log in as that user: the map, the alert
rail, and `GET /api/v1/alerts` all shrink to that subtree. Attempt a route or
preview on an out-of-scope camera → 403, and the denial itself is an audit
entry.

**Proves:** the BFF forces `org_scope` from the principal; the registry
predicates on `ltree path <@ scope`. Scope is not a UI filter — the same
predicate gates SSE, history, ack, preview and trace.

**Invariant:** *audited access, scoped to the caller's subtree.* The attempt
is evidence too.

## Beat 6 — verify the chain

**Do:** `/admin` → audit viewer → verify (or
`GET /api/v1/audit/verify` as admin). Response: `ok`, `first_broken_entry`,
`head_hash`, `row_count`.

**Proves:** every login, denial, trace, export and ticket above is a
hash-chained entry: `sha256(canonical(entry) + prev_hash)`. Edits and
mid-chain breaks are caught by name.

**Say the limit out loud:** tail truncation verifies clean — `audit/head`
exists so the tip can be anchored externally, and WORM export is deferred
(`docs/SECURITY.md` §2, `NEXT-PHASE-PLAN.md`).

## What this demo does not show

- **Measured scale.** `streams_per_gpu` is an unverified estimate until the
  `profile=gpu` run happens; `docs/SCALE-80K.md` labels it so.
- **Evidence clip pull.** Preview tickets are the audited video path that
  exists; nothing stores edge segments yet (`evidence_ref` unpopulated).
- **Keycloak SSO.** `auth.kind=keycloak` works end-to-end
  (`docs/KEYCLOAK.md`) but is off in both profiles — builtin login is the
  demo path.
- **Prometheus alerting.** Metrics endpoints exist; no alert rules do
  (`docs/OBSERVABILITY.md` "Known gaps").
