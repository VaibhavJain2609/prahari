# PRAHARI — Next-Phase Plan

Post-submission hardening and growth plan. Produced by a 16-agent audit:
12 domain specialists (contracts, each service, web, Helm, Terraform/tooling,
security, docs, tests) followed by 4 independent critics (security, DevOps,
architecture/invariants, UX) who re-verified every material claim against the
code and challenged the design directions. Findings below are the subset that
survived cross-verification; where a critic overturned a claim, the correction
is noted.

Date: 20 Sep 2026. Submission (7 Sep) and event (10–11 Sep) have passed; this
plan optimizes for a credible, deployable platform rather than demo-day beats.

---

## 0. Executive summary

The codebase is disciplined at the unit level — the core invariants (fuzzy
matching, PTS timing, loop-epoch tamper scoping, read-time staleness, lazy model
imports, workers-observe/registry-decides) are genuinely honored and well
tested. The failures are almost all at the **seams**: chart↔settings parity,
service↔service trust boundaries, deployability of the Day-3 slice, and the
audit/evidence chain.

Three load-bearing facts to internalize:

1. **`make up` does not currently produce a healthy deployment.** The
   match-engine watchlist env points at a directory nothing mounts
   (`_helpers.tpl` sets `PRAHARI_MATCH_WATCHLIST_DIR=/var/lib/prahari/watchlist`;
   no volume exists; the image bakes the data at `/app/data/watchlist`) →
   `/readyz` 503s → `helm --wait` hangs. Enabling `correlation`/`bff`/`web`
   deploys pods that can never work (missing env blocks, port mismatches, no
   Dockerfiles).
2. **The "every video access is audited" invariant is violated by design, not
   omission.** Camera list responses ship direct MediaMTX pull URLs *and*
   unauthenticated upstream gateway URLs; MediaMTX has no auth; there is no
   evidence-pull path at all; `GET /api/v1/streams/paths` returns decrypted DVR
   credentials to any caller.
3. **The internal plane is unauthenticated end to end.** `internal_token`
   defaults empty and no chart sets it; enabling it today breaks inference and
   correlation (they send no token); gRPC :9001 is plaintext; Redis has no auth;
   any pod can forge detections → forged alerts on police consoles, or spoof
   heartbeats that mark sabotaged cameras healthy.

Everything else is downstream of those.

---

## 1. P0 — Fix what's silently broken

These are bugs where the system is *wrong while looking green* — the exact
failure class the project exists to prevent. Each is small; none is optional.

### Chart & deployment correctness

| # | Bug | Fix |
|---|---|---|
| 1.1 | `PRAHARI_MATCH_WATCHLIST_DIR` → unmounted path → `make up` hangs | Point at baked `/app/data/watchlist` or add the ConfigMap/PVC mount (`_helpers.tpl`, `values.yaml`) |
| 1.2 | Postgres Secret `randAlphaNum` re-rolls every `helm upgrade` → total outage on existing PVC | Out-of-band Secret (same pattern as `prahari-gateway`) or `lookup`-guarded generate-once (`infra.yaml:10-21`) |
| 1.3 | Correlation gets no `PRAHARI_CORRELATION_*` env → consumer never starts; port 8002 (chart) vs 8003 (code default) vs `bff/config.py` default 8003; `registry_base_url` default `http://registry:8000` vs Service `prahari-registry` | Add `correlationEnv` block; pick ONE port; fix default host |
| 1.4 | BFF gets no env block; `session_cookie_secure=True` default means **the deployed console cannot log in over HTTP**; `audit.db` has no PVC (invariant: no local disk outside PVC) | `bffEnv` block + audit PVC + per-profile `session_cookie_secure` |
| 1.5 | Web proxy default `PRAHARI_BFF_URL=http://localhost:8001` — wrong host AND port (8001 is match-engine) | Fix default; chart sets `PRAHARI_BFF_URL=http://prahari-bff:8080` |
| 1.6 | No Dockerfiles for `bff`, `correlation`, `web`; `make images` builds 3 of 6; `make images` doesn't depend on `make proto` (gitignored stubs → fresh-clone image ImportError) | 3 Dockerfiles on the existing pattern; `images: proto` prerequisite |
| 1.7 | Inference image never installs the `models` extra → first real batch dies `ModuleNotFoundError`; no CUDA variant exists despite `decodeBackend: nvdec` | `--extra models` (build-arg gated); a real GPU Dockerfile variant before `profile=gpu` means anything |
| 1.8 | KEDA ScaledObject watches Redis **list** `prahari:frames:pending` — nothing produces it, and the bus is Streams not lists | Repoint to a real trigger (`redis-streams` on `prahari:detections`, or Prometheus once metrics exist) |
| 1.9 | Dead knobs: `bus.kind` (no Redpanda exists, nothing reads `bus_kind`), `ingest.maxActiveCameras`, `PRAHARI_PROFILE`, `PRAHARI_AUDIT_*`, `PRAHARI_REGISTRY_URL`, `PRAHARI_MEDIAMTX_RTSP/HLS` | Wire or delete each; extend the chart↔settings parity test (match-engine pattern) to `commonEnv`, `registryEnv`, `correlationEnv`, `bffEnv`, `ingest` — the class can't regrow once asserted |
| 1.10 | `make lint` targets `infra/terraform/envs/dev` (doesn't exist; `|| true` hides it); `helm lint` covers only values-local; `make verify` renders but never diffs | Point at `envs/demo`, drop `|| true`, lint both profiles, add a real diff or semantic assertion to `verify` |
| 1.11 | Tilt sets `imageTag=tilt` but never builds match-engine → ImagePullBackOff; Tilt port-forwards only registry:8000 | Add match-engine `docker_build` + port-forwards for bff/web/mediamtx |
| 1.12 | `mediamtx-config.yaml` hardcodes `apiAddress: :9997` vs templated `apiPort`; `PRAHARI_MEDIAMTX_PUBLIC_HOST` hardcoded cluster-internal (unresolvable to browsers) | Template both; make `publicHost` a values field |

### Service correctness

| # | Bug | Fix |
|---|---|---|
| 1.13 | Correlation fail-open: missing camera location → hop appended `PLATE@1.0`, indistinguishable from a gated hop, exported into evidence CSV/PDF | New `LinkKind.UNVERIFIED` (or explicit `confidence=0` + flag); surface in export |
| 1.14 | `RouteHop.location` typed `string` in `web/api.ts`; backend sends `{lat,lon}` → React render crash on the headline flow | Fix type; render polyline (see §5) |
| 1.15 | Audit chain forks under concurrency (`SELECT prev_hash` + `INSERT` unserialized on a shared connection) → `verify()` fails under normal load | `asyncio.Lock` or single-writer queue in `AuditLog` |
| 1.16 | No timeout on `StreamDetections`; no `socket_timeout` on Redis publishers → hung peer wedges pump/flush threads silently | `timeout=` on the RPC; `socket_(connect_)timeout` on `redis.from_url` |
| 1.17 | `model._load()` lazy-init races across concurrent batch threads; YOLO `predict()` called without `device=` → ultralytics silently auto-sniffs CUDA (the forbidden pattern, hidden in a library) | Lock around `_load()`; wire `device=`/`decode_backend` end-to-end or delete the knob |
| 1.18 | Registry `_loop` startup pass outside try/except → transient DB error kills sync forever; `Heartbeat.observed_at` unclamped → far-future timestamp permanently suppresses staleness | Move inside try; clamp `observed_at` server-side |
| 1.19 | `login/page.tsx` uses `useSearchParams` without `<Suspense>` → `next build` failure | Wrap or make route dynamic |
| 1.20 | `worker.main()` crashes if registry is down at boot | Retry/empty-start, let reconciliation fill |

---

## 2. Security posture

### Phase S1 — close the open plane (before any new feature)

The threat that matters for this system is **evidence integrity + surveillance
abuse**, ranked accordingly:

1. **Strip stream URLs from read models.** `Camera.endpoints` must not carry
   upstream `rtsp_url`/`hls_url`/`whep_url` to ordinary readers — for catalogue
   cameras those are *unauthenticated government-gateway URLs* no amount of
   MediaMTX auth can protect. The reconcile path reads the DB directly and
   doesn't need them in API responses. Serve browsers only BFF-mediated access
   (see §2.3).
2. **Remove or gate `/api/v1/streams/paths`** — it returns decrypted DVR
   credentials; its own docstring claims it doesn't. The MediaMTX `:9997` API
   leaks the same credentialed `source` URLs and must be auth'd too.
3. **Arm `internal_token` for real.** New out-of-band Secret (the
   `prahari-gateway` pattern) carrying `PRAHARI_INTERNAL_TOKEN` +
   `PRAHARI_CREDENTIAL_KEY` + bootstrap admin; `hmac.compare_digest` in the
   middleware; **fail-closed** when `profile != local` and token empty; add
   token fields + headers to the inference `RegistryClient` and correlation
   `RegistryClient` (both lack the field entirely). Gate correlation
   `/routes/{plate}` and match-engine `/alerts`, `/watchlist/reload` behind the
   same token.
4. **NetworkPolicies** — the compensating control `registry/config.py:73`
   references and the chart never ships: deny-all ingress to registry,
   correlation, match-engine :9001/:8001, redis, postgres, mediamtx :9997,
   except from named callers.
5. **Auth on the bus.** Redis `requirepass`/ACL + `rediss://`, or treat the bus
   as untrusted with HMAC-signed envelopes. Today any pod can `XADD` a forged
   `Alert` (reaches consoles via SSE) or forged `VehicleDetection` (fabricates
   route evidence).
6. **Heartbeat endpoint** — covered by (3); also add `Field` bounds on
   `Heartbeat` numerics and the `observed_at` clamp from 1.18.
7. **gRPC**: token-in-metadata interceptor on `MetadataIngestService` now; mTLS
   later (mesh is disproportionate at this scale — see §7 deferrals).

### Phase S2 — audit-chain trustworthiness

1. Serialize appends (1.15) and **write audit before returning the response**
   (fail-closed: if append fails, the access must not silently succeed).
2. Record the real outcome — today upstream 404/500s are logged as `read`.
3. **Audit admin/config actions** — user/key/org creation, camera
   create/update/delete/import, sync triggers. The log currently records
   evidence reads but not privilege grants — backwards for an accountability
   log.
4. PVC for `audit.db` (chart side of 1.4); note the single-writer constraint
   pins the BFF at 1 replica — that's acceptable; document it.
5. **Tail-truncation defense**: `verify()` can't detect deleted tail rows. Add a
   signed head/checkpoint (e.g., a periodic signed digest row or an external
   anchor). Acceptable interim: scheduled `verify()` + an admin surface.
6. Audit search endpoint (`GET /api/v1/audit`, admin, filtered/paginated) —
   `AuditLog.recent()` exists unrouted.

### Phase S3 — hygiene backlog

- Store `sha256(session_id)` not the raw id; `__Host-` cookie prefix; idle
  timeout policy.
- Constrain API-key minting (non-admin keys can't create admin keys;
  `created_by` attribution); add `GET`/`revoke`/`disable` routes for
  users/keys (repo methods exist).
- Login throttling + dummy `verify_password` on unknown-user path (timing
  oracle); `Origin` check on mutating endpoints (login-CSRF).
- Probe hardening: port allowlist (554 + small set), total-bytes bound,
  `LimitOverrunError`/`ValueError` → `ProbeError`.
- `plate` quoting in `Content-Disposition` and upstream path; CSV formula
  injection (`=`-prefixed cells); ReportLab mini-HTML injection in `export.py`.
- Security headers (CSP/nosniff/frame) on BFF + `next.config.ts`.
- `asyncpg.DataError` → 422/404 global handler (UUID cast 500s).
- SSE resource cap (per-connection Redis consumer + shared-executor
  starvation); CSV body size cap.
- Container hardening: pod `securityContext` (runAsNonRoot,
  readOnlyRootFilesystem, drop ALL, seccomp), image digest pinning,
  `redis:7-alpine` → pinned tag.
- Model weights: ultralytics downloads `yolov8n.pt` at runtime — unauthenticated
  supply-chain fetch; vendor or pin by hash.
- `pip-audit`/`osv-scanner` over `uv.lock`; `gitleaks` over history before any
  further public exposure.
- k3d host-maps MediaMTX ports to the laptop's LAN — document or restrict.

### Phase S4 — audited video access (the invariant made real)

Today no evidence-pull path exists and live video bypasses the audit entirely.
The credible design (verified against MediaMTX 1.9.3 capabilities):

- `authMethod: jwt` + `authJWTJWKS` on MediaMTX; BFF mints short-TTL JWTs scoped
  `mediamtx_permissions: [{action: read, path: cam-<id>}]`. **Ticket issuance IS
  the audit event** — cryptographically bounded access, not just logged access.
- Workers get a service identity (`authInternalUsers` or service JWT) with
  `read` on `cam-*`; the `:9997` API gets an `action: api` identity or
  `authJWTExclude`.
- WHEP preview uses a minted ticket; RTSP clients pass it as the password field.
- True evidence-pull (clip of recorded video) is deferred: feeds are live-only
  by design and nothing stores edge video (`evidence_ref` is never populated).
  Order of work when it comes: edge ring-buffer/DVR integration → signed
  capability `(camera, range, purpose)` minted+audited by BFF → fetcher redeems.
  `evidence.proto` describes the capability/manifest, not pixels — and only
  then.

---

## 3. Keycloak (mandated feature)

**Honest caveat, recorded:** both the UX and DevOps critics flagged Keycloak as
disproportionate for the current codebase — the local argon2/session/API-key
auth already demos RBAC, and `ORG-TIERS-DESIGN.md` defers IdP deliberately. It
is in this plan because it is an explicit requirement. The design below keeps
the migration low-blast-radius and preserves a non-SSO path.

### Architecture

- **Deployment follows the `bus.kind` pattern**: `auth.kind: builtin|keycloak`.
  `local` profile renders an in-chart Keycloak Deployment (laptop
  self-containment invariant); `gpu` points `auth.keycloak.url` at an external
  instance. Realm JSON as a ConfigMap (`--import-realm`); client secrets via a
  new out-of-band Secret (`kubectl create secret generic prahari-oidc` — same
  flow as `prahari-gateway`). **Not** the Keycloak Operator (a CRD lifecycle for
  one realm is disproportionate) and **not** Terraform-managed realm config
  (wrong plane — Terraform provisions capacity, not app config).
- **Flow**: OIDC Authorization Code + PKCE. Next.js route handlers
  (`/api/auth/login`, `/api/auth/callback`) or a BFF `/auth/oidc/*` pair drive
  the redirect; on callback, the BFF validates the ID token against the realm
  JWKS and **mints the same opaque `prahari_session` cookie** it issues today.
  This is the critical decision: keep the server-side session row (keyed on
  Keycloak `sub`) rather than exposing access tokens to the browser, because
  `EventSource` can't set headers (SSE alerts depend on the cookie), `proxy.ts`
  and all cookie plumbing survive unchanged, and `disabled_at`/`revoked_at`
  semantics — checked on every resolve — keep working.
- **Claim mapping (validated, never trusted)**: Keycloak realm roles
  `viewer|operator|admin` → `Role`. `org_path` stays **authoritative in
  Postgres** (`users.org_id → orgs.path` keyed on `sub`); if a claim carries
  `org_path`, use it only as a consistency check and validate the ltree-label
  alphabet — `in_scope` is string-prefix logic, so a forged claim must fail
  closed.
- **Stays local**: `pk_*` API keys (edge devices/ONVIF agents/vendor adapters —
  the purpose taxonomy is DB semantics Keycloak client-credentials doesn't
  carry); the org tree (ltree subtree predicates can't be expressed as KC
  groups); the audit log; the **bootstrap admin** (break-glass during IdP
  outage — keep, document, don't remove).
- **JWT hygiene**: pin `RS256`, enforce `iss`/`aud`/`azp`, `kid`-tolerant JWKS
  cache, bounded clock skew. Never forward user JWTs to registry/correlation —
  `aud` would be wrong; service-to-service stays the internal token (or
  Keycloak client-credentials later).
- **Logout**: revoke local session AND RP-initiated logout to Keycloak
  `end_session_endpoint`, else "Sign out" silently re-authenticates.
- **Settings**: `PRAHARI_OIDC_ISSUER_URL`/`CLIENT_ID`/`CLIENT_SECRET`/
  `JWKS_URL` on `BFFSettings`, plus the repo's own invariant test — a
  `test_bff_settings.py` chart-parity test so `PRAHARI_OIDC_*` can't become the
  next `PRAHARI_BUS_KIND`.
- `users`/`sessions` tables remain (session rows get a `sub` column; user
  provisioning can JIT-create on first login or sync from Keycloak).

---

## 4. DevOps

The asked-for heavy focus. Current state, then additions.

### What exists (verified)

- k3d cluster (k3s `v1.31.4`, registry `:5555`, host port maps — **currently
  dead**: chart Services are ClusterIP, nothing bridges them).
- Tilt inner loop (same chart, live-reload on registry; tag bug + missing
  port-forwards per 1.11).
- Helm umbrella chart: hand-rolled postgres/redis/mediamtx deps (deliberate),
  generic service loop, inference Deployment + KEDA ScaledObject, `profile`
  value + `values-{local,gpu}.yaml`.
- Terraform `modules/district`: AWS VPC/subnet/SG/`g6.xlarge` nodes +
  cloud-init k3s bootstrap; `envs/demo` + `envs/dev` (empty).
- Makefile: proto/lint/test/verify/images/up/down/gateway-secret; `buf lint` +
  `proto-breaking` targets exist.
- uv workspace, committed `uv.lock`, Python pinned 3.12, reproducible
  `--frozen` builds.
- ~48 test files / ~418 tests, hermetic (fakes, loopback gRPC, ASGI), two real
  gate tests.

### What's broken/absent (all verified)

- **No CI whatsoever** — no `.github/`, no pre-commit, no secrets scanner
  despite "nothing that fails the secrets scan" being a stated gate.
- **Terraform module cannot bootstrap**: no IGW/route table/`map_public_ip`/
  NAT; SG has zero ingress and no DNS(53)/HTTP(80) egress; every node runs its
  own k3s server (no join) — count>1 yields N separate clusters. No remote
  state, no IAM profile, no ECR, no LB/DNS/TLS.
- **Zero observability**: no `prometheus_client`/OTel anywhere, no `/metrics`,
  no ServiceMonitor, MediaMTX metrics off, stdout-only logs. Also means
  SCALE-80K numbers cannot be produced.
- **`infra/loadtest/` is an empty dir**; `docs/SCALE-80K.md`, `SECURITY.md`,
  `COST-MODEL.md`, `DEMO-SCRIPT.md`, `HLD.md` are all referenced but absent.
- No ingress; no PDBs; no NetworkPolicies; no securityContexts; no resource
  limits on postgres/redis/mediamtx; no backups; `audit.db` no PVC; no
  release/version strategy (everything `imageTag: dev`, Chart.yaml 0.1.0
  forever); no dependabot; no `.dockerignore` (`.env` ships as build context);
  `PRAHARI_GATEWAY_DIRECT_HOST`/ports land in the Secret but are never
  consumed; no KEDA-CRD prerequisite handling (`helm install` on a KEDA-less
  cluster fails when `keda.enabled`).

### DevOps work-breakdown (sized, sequenced)

**D1 — CI pipeline (S; highest leverage after P0).** One GitHub Actions
workflow: `buf lint` + `buf breaking` + `buf generate` → `uv sync --locked` →
`ruff check`+`format --check` → `pytest -q` → `helm lint`×2 profiles +
`helm template`×2 → `docker build`×5 (post-proto) → `npm ci && next lint &&
tsc --noEmit && next build` → `terraform fmt -check` + `validate` on
`envs/demo` → `gitleaks`. Follow-up stages (post-MVP): `trivy` image scan,
`syft` SBOM, `cosign` keyless sign. Sequencing note from the critic: land the
P0 chart fixes *first* or CI is permanently red — the parity tests are the
point.

**D2 — Helm backlog.** H1 dead-knob purge/wire + extended parity tests; H2
postgres Secret fix; H3 watchlist mount; H4 correlation env/port; H5 bff env +
audit PVC + security Secret; H6 web env; H7 KEDA trigger repoint + declare the
CRD prerequisite; H8 MediaMTX probes/metrics/templated apiAddress/publicHost;
H9 stateful-tier resources + securityContexts + pinned redis; H10 release-name
composition or explicit single-release guard; H11 connectivity story
(NodePort/port-forward docs for k3d; `ingress:` values block + Ingress
template for cloud — Traefik is disabled and no controller is installed, so
this needs a home); H12 `values.schema.json` (`profile ∈ {local,gpu}` etc.) +
`NOTES.txt`.

**D3 — Terraform.** T1 networking minimum (IGW + routes + public IP; SG
ingress 22-restricted + 6443; egress 53/80). T2 topology honesty: **rescope to
single-node** and document (demo math never produces count>1), OR implement a
real join via SSM/Secrets-Manager token handoff — recommend rescope. T3 S3
backend + lockfile. T4 outputs/runbook (`helm upgrade` targeting). Later:
ECR, IAM, central-plane module, ExternalSecrets.

**D4 — Observability (right-sized).** O1 `prometheus_client` + `/metrics` on
registry/match-engine/correlation/bff; inference exposes counters (streams,
detections/s, drop counts, batch latency) on a tiny HTTP port. O2 MediaMTX
`metrics:` + scrape annotations. O3 kube-prometheus-stack as a **separate
release** on the gpu cluster only. **Skip OTEL collector** (no backend, no
spans worth correlating) and log shipping (`kubectl logs` suffices) — both
documented deferrals.

**D5 — Loadtest harness** (`infra/loadtest/`): synthetic camera farm (MediaMTX
paths fed by looped clips), stepped camera-count driver, metrics capture →
`runs/` summaries → `docs/SCALE-80K.md`. The module's `streams_per_gpu`
already feeds Terraform math, so the measurement and the claim can't drift.

**D6 — Release & supply chain:** immutable image tags (`git-sha`), Chart.yaml
version bumping, dependabot (uv/npm/docker/github-actions/terraform),
`.dockerignore`, `make images: proto` dep, secrets scan in `make lint`.

**D7 — Postgres durability:** `pg_dump` CronJob or documented exec-runbook;
audit.db PVC (H5). WAL-G/Velero deferred with reasons.

**Explicitly deferred (with reasons):** GitOps/ArgoCD (one release, one
namespace — premature), SOPS (public repo + solo team), Keycloak Operator,
multi-node k3s clustering, OTEL, WAL-G, managed Postgres.

---

## 5. Functionality

### Alerts — persist first, lifecycle-lite second

- **Store**: `alerts` + `alert_events` tables via the registry's migration
  runner (the `006_identity.sql` precedent — BFF-owned tables, checksummed
  runner). A BFF-side consumer of `prahari:alerts` (same decode as the SSE
  relay) writes them; Redis stays fan-out, Postgres is the record.
- **Ownership split** (architecture critic): match-engine owns the immutable
  alert record; **BFF owns workflow state** (`acknowledged_at`, actor, note) —
  same trust domain as identity/RBAC/audit. No third service.
- **Scope**: `acknowledged` only — one status, one button, into the audit
  chain. Skip assign/resolve/escalate (wrong size for the team). Full queue
  semantics later.
- BFF endpoints: `GET /api/v1/alerts` (scoped, paginated, filtered),
  `GET /alerts/{id}` (full `MatchExplanation`), `POST /alerts/{id}/ack`.
- Cap `prahari:alerts` with `XADD MAXLEN ~` (parity with detections).

### Correlation

- Fix fail-open (1.13), collapse consecutive same-camera sightings into one
  hop with dwell duration at **route assembly** (store stays evidence),
  dedup by `detection_id`.
- **Plate fragmentation**: key sightings by the confusion *skeleton* — move
  `skeleton()` to `prahari-common` (canonicalization is grammar, not
  tolerance; the grammar/tolerance split is preserved). Same vehicle joins
  even when OCR confuses `2/Z`.
- **Durability**: `sightings` Postgres table (Timescale hypertable when the
  extension is present — the existing graceful-degradation pattern), consumer
  checkpoint persisted (start from checkpoint, not `$`); consumer group only
  when a second reader exists.
- Clock-skew bound on `elapsed_s`; honor `loop_epoch` in stitching.

### Worker assignment (the real scaling fix)

Workers currently self-assign `first N active cameras` — every replica takes
the SAME N → KEDA adds duplicated heat, and two workers' heartbeats interleave
into one camera's health history (corrupting tamper-streak/fps-baseline math).
Fix: `assigned_to`/lease column or deterministic shard (`camera_id hash %
replica_count` via a registry-side assignments endpoint). This is the single
largest unexamined scaling defect.

### Dashboard data endpoints (backend gaps for §6)

- `GET /cameras/{id}/health-history` (table exists, no read endpoint) —
  uptime/sparklines.
- Per-org rollup (`/gaps/orgs`), pagination totals on `list_cameras`.
- BFF aggregation: `GET /api/v1/status` (fan-out to service `/readyz` +
  `/metrics` summaries); proxies for registry `/sync`,`/sync/runs` and
  match-engine `/watchlist/summary`; `GET /api/v1/audit` search.
- Keep aggregates as SQL views/read-time queries (staleness is read-time by
  invariant); Timescale continuous aggregates only when historical charting
  exists. **No `metrics.proto`** — REST is the sanctioned browser transport.

### Evidence & detection gaps (flag, don't build yet)

`evidence_ref` and `appearance_embedding` are never populated — the bridging
feature is dead end-to-end though tested. Decide deliberately: populate
embeddings (bigger scope, privacy gate per invariants) or remove the field
honestly.

### Proto/contract hygiene

- Reconcile health vocabularies: canonicalize `measured_fps` + `loop_epoch`
  (int) in `HealthEvent`/`camera.proto`; implement `StreamHealth` client-side
  or delete the RPC.
- Drop `gen/ts` (unconsumed) or wire protobuf-es.
- `make proto-breaking` into `lint`/CI; pin remote codegen plugin versions;
  `PlateFormat` IntEnum↔proto parity test.
- New protos only when a consumer exists: `alerting.proto` after the store
  decision, `identity.proto` **rejected** (REST-only, no second consumer),
  `evidence.proto` when the pull path exists.
- Reword the invariant honestly: "protobuf on the high-rate metadata plane;
  REST service-to-service elsewhere" — that's what's actually built.

---

## 6. UI/UX

The UX critic substantially revised the first wave's IA. Its reasoning: the
product's thesis is that the **map is the control surface** — a widget-gallery
dashboard moves the operator away from it; the highest-value rollups
(dark zones, coverage) are spatial anyway. Recommended final IA:

```
/login  → SSO redirect when Keycloak lands (keep local login as break-glass)
/(console)/layout.tsx   me() once → Principal ctx; header: nav, SSE status
                        dot, user menu, theme toggle; global 401→login; error
                        boundary
/                       OPS CONSOLE (landing, map-primary):
                        map + alert rail (priority, raised_at, click→camera)
                        + trace dock (purpose-code prompt, polyline on map,
                        ?trace= shareable) + camera detail drawer (audited)
                        + dark-zone overlay toggle + compact summary strip
                        (cameras/summary) above the sidebar
/cameras                filterable table (existing district/state/lifecycle/
                        search params); operator+: register, CSV import,
                        edit, decommission; row → same detail drawer
/alerts                 history + filters — ONLY once persistence (§5) lands;
                        otherwise keep the live rail and skip the route
/admin                  org tree, users+keys list/revoke (needs endpoints),
                        audit viewer + verify, sync status/trigger
```

Explicitly **not** built: standalone `/dashboard` page (folded into the strip
+ admin coverage tab), `/routes` page (a map mode), `/cameras/[id]` page
(drawer), `/onboarding` route (folded into `/cameras`), org switcher (users
have one org; subtree focus is a filter), alert assignment.

### Prioritized UX work list

| # | Item | Size |
|---|---|---|
| 1 | Fix `hop.location` crash + draw route polyline on map (solid plate / dashed bridged / dark-zone markers — all already in the payload) | S |
| 2 | Alert→workflow: click flies to camera, opens drawer, prefills trace; render priority/reason/`MatchExplanation` (currently discarded) | M |
| 3 | Global 401→`/login?next=` + error boundary per panel | S |
| 4 | Dark-first theme toggle + dark basemap variant (ops rooms are dark) | S |
| 5 | Purpose-code prompt (case-ref input) on trace/detail/export — hardcoded constants currently defeat the audit design | S |
| 6 | Camera detail drawer (audited `GET /cameras/{id}`; PATCH/DELETE operator+) | M |
| 7 | `/cameras` table + fold in onboarding; CSV template download, pre-validation, failed-rows retry export | M |
| 8 | Map clustering + `bbox` on move (silently caps at 20k today) | S |
| 9 | Systematic skeleton/empty/error states + first-run "sync or import" estate-empty state | S |
| 10 | a11y pass — zero `aria-*`/`htmlFor` today; procurement will check | S |
| 11 | Alert freshness (`raised_at` + time-ago), CRITICAL sound/browser notification | S |
| 12 | `/login` Suspense fix | XS |
| 13 | Admin: list/revoke/disable + audit viewer + sync trigger | M |
| 14 | SSE reconnect indicator + alert history preload | S |
| 15 | WHEP preview in drawer — gated on MediaMTX auth + reachable endpoint (§2.S4) | M–L |
| 16 | URL-driven state (`?trace=`, `?camera=`, viewport) for shareable dispatch links | S–M |
| 17 | Sidebar collapse/responsive | M |
| 18 | "Sync now" affordance (makes zero-code onboarding clickable) | S |

### Web quality infra

vitest + testing-library + playwright (already an optional Next peer); wire
`npm lint`/`tsc --noEmit`/`build` into `make lint`/CI; OpenAPI→TS contract
check for `api.ts` (hand-mirrored types are a live drift risk — the
`hop.location` bug is proof).

---

## 7. Sequencing

```
Wave A — deployability & truth (P0 §1 + D1 CI + D2 chart fixes)
          "make up" healthy end-to-end; CI red→green ratchet on
Wave B — close the plane (S1 §2: token arming, netpols, mediamtx auth,
          URL stripping, audit fixes; S3 quick wins)
Wave C — Keycloak + console (§3 auth.kind=builtin→keycloak; §6 items
          1–8; dashboard strip + /cameras + /admin)
Wave D — scale & durability (worker assignment, correlation Postgres,
          D4 observability, D5 loadtest → SCALE-80K.md; alert persistence
          + /alerts page; TF rescue if cloud run resumes)
Wave E — deferred-by-design (evidence pull, embeddings, Redpanda-or-delete,
          full alert queue, GitOps) — each gated on a real need
```

Every wave ends with the repo's own loop: `make proto && make test &&
make lint && make verify` — plus CI as the merge gate from Wave A on.

## 8. Verification additions per feature

- Chart↔settings parity tests generalized to **all** env blocks (the dead-knob
  class can't regrow).
- Keycloak: mocked JWKS via `httpx.MockTransport` (house pattern), test RSA
  JWTs → `get_principal` claim→Principal mapping; expired/wrong-aud/revoked-kid
  negatives; break-glass login test.
- Dashboard: OpenAPI snapshot tests per service; Playwright happy path
  (login→map→alert→trace→export).
- Security regression: audit-fork test (concurrent appends), scope-strip test
  (`org_scope` forgery), heartbeat `observed_at` clamp test, `/streams/paths`
  credential-absence test, SSRF mapped-IPv6 + port-allowlist tests,
  `/healthz`/`/readyz` info-leak test.
- `test_org_tiers_gate.py` — specified in ORG-TIERS-DESIGN §6, never written.

## 9. Corrections the critics issued to Wave 1 (for the record)

- `use_hls` **is** wired (config→worker→capture); TODO.md was stale.
- `camera.proto` field 12 is `native_height`, not a skipped tag — no
  `reserved` needed.
- IPv4-mapped-IPv6 SSRF bypass is closed on Python ≥3.12.6 (current images);
  port-scan oracle and unbounded body read remain real.
- gRPC does have a default 4 MiB message cap; the real DoS is thread-pool
  exhaustion via long-lived unauthenticated client streams.
- `PRAHARI_MEDIAMTX_*` envs are live for the **registry** (only the two
  inference-side ones are dead).
- A standalone `/dashboard` landing and `/routes` page were over-proposed;
  map-primary IA adopted instead (§6).
