# Day 3 design — route reconstruction, BFF, console, export

Referenced from `TODO.md` ("Day 3 — 4 Sep"). `docs/PLAN.md` is the *why* for the whole
project; this is the *how* for one day's worth of it. Written before the code, so the
reviews have something to review against.

**Day 3 gate:** the full vertical slice runs on the laptop — registry, inference,
match-engine, correlation, BFF, console — and the mandatory test case works end to end:
give it a registration number, get back a timestamped, location-wise route. This is an
explicit go/no-go: it decides whether Day 4 spends GPU money.

---

## 1. What Day 2 left on the table, and where it lands here

| Day 2 gap | Closed by |
|---|---|
| Match-engine publishes **alerts only** — a plate that never hit the watchlist leaves no trace on the bus | §2 — a `prahari:detections` stream, every detection with a legible plate, watchlist hit or not |
| No consumer anywhere reads `appearance_embedding` | §3.3 — correlation's gap bridging, scoped honestly (see limitation below) |
| No audited path to camera/route data | §4 — BFF auth, RBAC, purpose codes, hash-chained log |

**The mandatory test case does not require a watchlist hit.** An evaluator can hand over
any plate. Day 2's bus only carries what matched, so route reconstruction for a
non-watchlist plate is currently impossible — not degraded, impossible, because nothing
was ever recorded. This is the load-bearing fix Day 3 opens with.

---

## 2. The detections bus — `services/match-engine` change

`MetadataIngestServicer._handle_detection` (`grpc_server.py`) currently returns early on
`not detection.HasField("plate")` and again on `not result.matched`. Both returns are
correct for *alerting* and wrong for *evidence*: they mean a non-hit is never durable
anywhere, and querying "where has this plate been" only works in hindsight for a vehicle
that happened to be on the watchlist at detection time.

**Change:** publish every `VehicleDetection` — legible plate or not — onto a second Redis
Stream, `prahari:detections`, *before* the watchlist-match branch and independent of its
outcome. Same shape as `alerts.py`: a `DetectionPublisher` Protocol, a
`RedisStreamPublisher` implementation (lazy-connect, log-and-swallow on failure — a Redis
blip must not fail the gRPC ack), `XADD ... MAXLEN ~ N` so an unbounded producer cannot
grow the stream forever on a long-running demo.

This is exactly the metadata-plane traffic the architecture already budgets for
(`CLAUDE.md`: "detections, plates, tracks, alerts flow centrally as protobuf events") —
not a new category of traffic, just a consumer that didn't exist yet.

**Settings addition to `MatchSettings`:** `redis_detection_stream_key: str =
"prahari:detections"`, `detection_stream_maxlen: int = 200_000`. Reuses the existing
`redis_url` — one Redis, two streams, same "`None` means no bus fan-out" rule as alerts.

---

## 3. `services/correlation` — route reconstruction

Owns the mandatory test case. Input: a scatter of `VehicleDetection` across cameras and
time, read off `prahari:detections`. Output: a defensible, honestly-uncertain route.

### 3.1 Detection store

A background consumer (`bus.py`, new shared helper in `prahari-common` — see §3.5) reads
`prahari:detections` and indexes by `normalised_text` (via `prahari_common.plates`, the
same grammar inference uses — **never re-derive plate parsing here**). In-memory, bounded
ring buffer per plate key, capped total size. This is a laptop-scale demo store, not a
database: Day 5's load test is explicitly out of scope for this store's design, and a
real deployment would put this behind Timescale the way heartbeats are. Noted as a known
limitation, not hidden.

### 3.2 Feasibility gating

For each consecutive pair of detections assigned to the same plate: fetch both cameras'
`GeoPoint` from the registry (`GET /api/v1/cameras/{id}`, cached with a short TTL — the
registry is the source of truth for camera location, correlation does not duplicate it),
compute great-circle (haversine) distance, and reject the hop if the required average
speed exceeds a configured envelope (`max_speed_kmh`, default 120).

**Known limitation, stated plainly:** this is great-circle distance, not road-network
distance. `correlation-engineer.md` asks for road-network distance; a real routing engine
(OSRM/Valhalla) is out of scope for the hackathon's time budget. Haversine is a strictly
*more permissive* proxy (road distance ⩾ great-circle distance for any non-straight road),
so the gate is conservative in the safe direction — it can pass a hop that a road engine
would reject, never the reverse. That asymmetry, and the reason for it, goes in
`docs/SCALE-80K.md`'s limitations section, not just here.

### 3.3 Gap bridging with appearance

Where the plate is unreadable on a detection but `appearance_embedding` is present,
bridge two plate-confirmed segments across it with cosine similarity against a
configurable threshold, **only when the intervening detection is also feasibility-gated**
against both neighbours. Labelled `bridged` in the output, distinctly and always — never
silently merged with a plate-confirmed hop. `appearance_embedding` is low-dimensional by
contract (`events.proto`); no re-ID model is trained or shipped, this is a cosine
threshold over whatever the inference cascade already emits.

### 3.4 Route assembly and output contract

Ordered, timestamped hops. Each hop carries: `camera_id`, `location`, `observed_at`
(wall-clock **and** `pts_ms`, per `StreamTime`'s own invariant), a link kind (`plate` |
`bridged`), a confidence, and an `evidence_ref`. Blind corridors — road segments the
registry's gap analysis already reports as uncovered — are surfaced between two hops
rather than silently interpolated across, using `services/registry`'s existing
`/api/v1/gaps/dark-zones`.

### 3.5 Shared bus helper (`packages/prahari-common`)

`prahari_common/bus.py`: a thin `RedisStreamConsumer` (blocking `XREAD`, tracks last-seen
ID, decodes a protobuf message via a caller-supplied factory). Both correlation
(detections) and BFF (alerts, §4.3) need "read a Redis Stream of protobuf messages" and
nothing service-specific; putting it in `prahari-common` means the decode-and-track-offset
logic is written once, matching why `plates.py` lives there.

### 3.6 Surface

REST only — correlation is not on the gRPC high-rate path, its callers are BFF and (for
gap data) itself calling registry.

- `GET /api/v1/routes/{plate}` — the reconstructed route. `plate` is matched against
  `normalised_text`, using `prahari_common.plates` normalisation so a caller typing
  `GJ 01 AB 1234` and one typing `gj01ab1234` hit the same key.
- `GET /healthz`, `/readyz` — `/readyz` reports whether the detection consumer is actually
  connected to Redis, same reasoning as match-engine's watchlist check: a correlation
  service that silently stopped consuming looks healthy and returns empty routes forever.

---

## 4. `services/bff` — auth, RBAC, audit, SSE

### 4.1 Auth and RBAC — scoped honestly for a hackathon timeline

No IdP integration in the time available. Static, department-scoped API keys
(`BFF_API_KEYS`, JSON: `{key: {subject, department, role}}`), `Authorization: Bearer
<key>`. Role is `viewer | operator | admin`. This is explicitly a seed for a real IdP
(SAML/OIDC against a government directory), not a claim that it is one —
`docs/SECURITY.md` (Day 5) says so in those words.

- **Department scoping.** A `viewer`/`operator` only sees cameras and routes where
  `Camera.department` matches their token's department, enforced server-side by filtering
  the registry response, not by hiding it client-side. `admin` bypasses scoping.
- **Cross-department access is a logged exception path**, not ambient: an `operator`
  requesting a camera outside their department gets `403` unless the request also carries
  `X-Cross-Dept-Grant`, a pre-issued token checked against a small allow-list — this is
  the audit chain's most important entry, per `security-privacy-auditor.md`.

### 4.2 Purpose codes and the hash-chained audit log

Every request that touches evidence-adjacent data — a route, a camera detail, an export —
must carry `X-Purpose-Code` (free-text investigation reference); its absence is `400`, not
a default. Each such request appends one entry to an audit log:

```
entry = {id, actor, department, purpose_code, resource, action, occurred_at, prev_hash}
hash  = sha256(canonical_json(entry) + prev_hash)
```

Append-only, `prev_hash` of the genesis entry is a fixed constant. `GET
/api/v1/audit/verify` walks the whole chain and reports the first broken link, if any —
this is the endpoint `security-privacy-auditor.md` asks to be able to demonstrate
("break a link, confirm verification detects it").

**Storage: SQLite, not a new Postgres schema.** The audit log is append-only,
single-writer, and never joined against camera/route data — a second schema in the
registry's Postgres would buy nothing but migration coordination between two services
that don't otherwise share a database. SQLite file under a PVC mount (wired in the Day 3
Helm work, §6) satisfies "no local disk state outside a PVC" the same way a Postgres PVC
would.

### 4.3 SSE relay

`GET /api/v1/alerts/stream` — reads `prahari:alerts` via the same `RedisStreamConsumer`
correlation uses for detections (§3.5), re-emits each `Alert` as an SSE event
(`MessageToDict`, matching match-engine's own JSON shape so the console does not maintain
a second mapping). Per-connection department filtering, same rule as §4.1.

### 4.4 Report export

`GET /api/v1/routes/{plate}/export?format=csv|pdf`, purpose-coded and audited like any
other route access (§4.2) — an export is the evidence-access case
`security-privacy-auditor.md` weights most heavily, not a side effect of a REST GET.
Calls correlation's `/api/v1/routes/{plate}` internally, then renders:

- **CSV** — one row per hop: plate, camera id, district, location, observed-at
  (wall-clock and `pts_ms`), link kind, confidence. Literal submission requirement:
  "detected vehicles/plates with corresponding timestamps."
- **PDF** — same rows, plus a `Provenance` block per hop (camera, timestamp, processing
  chain, content hash) rendered as a simple table. No charting library — `reportlab`,
  table-only, because the requirement is defensibility, not design.

---

## 5. `web/` — console

Next.js + MapLibre. Scope, deliberately narrow for one day:

1. **Map** — camera markers from registry `/api/v1/cameras/geojson`, coloured by
   `effective_health_state`. Polling, not SSE — health changes on the order of the
   staleness window (tens of seconds), not per-event.
2. **Alert console** — live feed off BFF's SSE endpoint (§4.3), newest first, priority
   colour, click-through to the alert's `MatchExplanation` (the edits, not just the
   score — this is the artifact that makes a match defensible).
3. **Plate trace** — a search box calling `GET /api/v1/routes/{plate}` (via BFF, purpose
   code required, prompted in the UI), rendered as a polyline on the map, `plate`-linked
   hops solid, `bridged` hops dashed. Export buttons (CSV/PDF) next to it.
4. **WHEP live preview** — one camera at a time, from `StreamEndpoints.whep_url`.
   **Preview only, never an inference source** — already an invariant, restated here
   because the console is the one place a reviewer could plausibly wire it in as one by
   mistake.

No auth UI beyond pasting a bearer token into a settings panel — a login flow is not a
Day 3 problem, and building one would spend the day on the wrong thing.

---

## 6. Deployment

Dockerfiles for `correlation` and `bff`, built from the workspace root like every other
service. Helm templates added to `infra/helm/prahari/templates/`: `correlation.yaml`,
`bff.yaml` (with a `PersistentVolumeClaim` for the SQLite audit file), `web.yaml`. Env
blocks follow the existing `PRAHARI_CORRELATION_*` / `PRAHARI_BFF_*` convention and get
the same chart-to-settings-field test every other service has (`DAY2-DESIGN.md §3`, rule
2) — a value the chart writes and nothing reads is worse than no value.

`make images` and `make up` gain the three new services. This is Day 3 scope, not Day 4:
the gate is "runs on the laptop," which means k3d, not the GPU node.

---

## 7. How this gets verified

Same loop structure as Day 2 (`DAY2-DESIGN.md §8`), one service at a time rather than in
parallel — sequential build-then-verify, since one person is driving today rather than a
dispatched team.

**Loop 1 — build.** Code plus unit tests per service. Exit: `uv run pytest` and
`ruff check` green for that service, before moving to the next.

**Loop 2 — cross-verification.** BFF gets a pass from `security-privacy-auditor` (RBAC,
purpose codes, chain integrity — its own domain). Correlation gets a pass against
`correlation-engineer.md`'s own checklist (feasibility gate actually rejects an injected
impossible hop; a bridged hop is never presented as a plate match). Neither review is
performed by the code's own author.

**Loop 3 — integration.** `make proto`, `make test`, `make lint`, `make verify` to
convergence. Then the Day 3 gate as an executable test: synthetic detections (including
one impossible-hop injection and one non-watchlist plate) through the detections stream →
correlation route → BFF export → CSV/PDF checked for the injected plate's true hops and
the rejected hop's absence. No model weights, no live gateway, no k3d cluster required for
the gate test itself — same reasoning as Day 2's gate: the gate proves wiring, not
infrastructure.

---

## 8. Deliberately not in Day 3

- Real road-network routing (OSRM/Valhalla) — haversine + speed envelope, limitation
  stated in §3.2 and carried to `SCALE-80K.md`.
- Any IdP integration — static API keys, stated as a seed in `SECURITY.md`.
- A trained re-ID model for appearance bridging — cosine threshold over the existing
  low-dimensional embedding, nothing more.
- Statewide RBAC hierarchy beyond department + admin — one cross-department grant
  mechanism, not a full org chart.
- k3d/Helm as the Day 3 gate's execution path — the gate is a pytest, like Day 2's;
  `make up` with the three new services is exercised but is not what "passing" means.
