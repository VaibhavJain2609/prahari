# TODO

**Deadline: 7 Sep 2026 — passed.** Today is 20 Sep. Event 10–11 Sep — passed.
Reasoning behind the ordering is in `docs/PLAN.md`; this file is the checklist
and now also the record of what the sprint actually delivered. Post-submission
work is tracked in `docs/NEXT-PHASE-PLAN.md` (lands on branch
`docs/next-phase-plan`).

Legend: **[you]** needs a human · **[blocked]** waiting on something ·
**[est]** contains an unverified number that must be replaced with a measurement.

---

## Blocking everything

- [ ] **[you]** Register on `sentinel.gujarat.gov.in`. Registration closes *with*
      submission on 7 Sep. Not a background task — nothing ships without it.
- [ ] **[you]** Put the gateway host + password into `.env` (shape in
      `.env.example`). Never into Helm values, tfvars, or a commit.
- [ ] **[you]** Capture the first `/api/ingest` snapshot into `data/catalogue/`
      (gitignored — it embeds the host).
- [ ] **[you, optional]** Email `sentinel.hackathon@gujarat.gov.in`: team size,
      registration fee, IP terms, finale hardware. None are stated on the site;
      the last two could change the Phase 2 plan.

## Unblocked by the first catalogue capture

- [ ] **[blocked]** Pin the real `/api/ingest` JSON field names. Parsing in
      `catalogue.py` is alias-tolerant with `raw` kept on every entry — that is
      scaffolding, not a design. Tighten it and delete the aliases.
- [ ] **[blocked]** Confirm how the access password is presented. `catalogue.py`
      currently sends it *both* as HTTP basic and as `X-Access-Password`.
      Find which earns a 200, delete the other.
- [ ] **[blocked]** Record the real camera count and codec mix. Drives batching
      shape and every capacity figure downstream.
- [ ] **[blocked]** Verify RTSP over TCP actually connects on this network. If
      8554 is blocked, switch to the HLS fallback and note the added latency.

---

## Day 1 — 2 Sep · registry, GIS, fan-out (local)

Written and unit-tested. **None of it has run against a real Postgres or the
live gateway** — see "Before Day 2 starts" below.

- [x] `services/registry/` — FastAPI + PostGIS. Camera CRUD, `/healthz` and
      `/readyz`. They are different endpoints on purpose: `/healthz` does not
      touch the database, because a liveness probe that fails on a database blip
      restarts every pod and guarantees a longer outage than the blip.
- [x] PostGIS schema + migration (4 files, applied on startup under an advisory
      lock and checksummed — an edited applied migration is rejected, not
      silently ignored). TimescaleDB hypertable + retention where the extension
      exists; `postgis/postgis` does not carry it, so the registry also prunes
      heartbeats itself. Bounded growth does not depend on the extension.
- [x] Catalogue sync: `/api/ingest` → registry. Idempotent, re-runnable, marks
      vanished cameras absent rather than deleting them (their detections are
      evidence), and never overwrites curated fields with nulls.
- [x] Populate MediaMTX paths from the registry at runtime. Only `cam-`-prefixed
      paths are ever removed, and an unreachable MediaMTX skips reconciliation
      rather than failing the sync — a restreamer restart must not stop camera
      onboarding.
- [x] Camera health: heartbeat + `measured_fps` drift against the camera's *own*
      recent median (never `CAP_PROP_FPS`, never `declared_fps`), feeding Model 1
      district coverage and dark-zone analysis.
- [x] Worker side of the health contract: `services/inference/worker.py` pulls
      its assigned cameras from the MediaMTX fan-out and heartbeats the registry.
      Workers report observations; the registry decides state.
- [x] `Tiltfile` — inner dev loop against k3d, rendering the same chart `make up`
      installs. Tilt applies no YAML of its own.
- [x] Dockerfiles for registry and inference, built from the workspace root.
- [ ] **Gate:** every camera visible on the map with live health. None analysed
      yet. **Not met** — the console exists now (Stage 5); what is still missing
      is a live catalogue and a real `make up` run.

## Before Day 2 starts

Day 1 is code-complete and, at the time this list was written, had never touched
a database. Since then the no-cluster path in `README.md` ("Quick local run")
has been verified end to end — real Postgres, real migrations, a real login —
but **the k3d path below has still never run**, and `docs/NEXT-PHASE-PLAN.md` §1
lists known chart bugs that will bite on the first `make up`.

- [ ] `make cluster && make images && make up` — first real run. Postgres cold
      start vs. the registry's startup probe, PostGIS extension creation, the
      migration advisory lock. (Known blockers first: NEXT-PHASE-PLAN §1.1–1.6 —
      unmounted watchlist dir, missing env blocks, missing Dockerfiles for
      bff/correlation/web.)
- [ ] Confirm `camera_current` returns `effective_health_state` correctly with a
      real `now()` — the staleness overlay is pure SQL and has no unit test.
- [ ] Register one camera by hand (`POST /api/v1/cameras`), heartbeat it, watch
      it go healthy then stale. This exercises the whole health path without the
      gateway.
- [ ] Kill the registry pod mid-heartbeat: workers must keep pulling and recover
      on the next report.

### Gaps Day 1 opened (three since closed; kept here struck-through as the record)

- [x] ~~**`PRAHARI_INGEST_USE_HLS` is documented and read by nothing.**~~
      Wired on Day 2: `IngestSettings.use_hls` (`inference/config.py`) is
      threaded to `StreamCapture(use_hls=...)` in `worker.py`, with tests
      (`test_use_hls_is_threaded_to_the_capture_construction`). One honest
      caveat, recorded in the field's docstring: it only affects
      catalogue-derived URLs — a cluster worker is handed an explicit MediaMTX
      fan-out URL and ignores the flag. The fallback is real but has still
      never met a network where 8554 is blocked.
- [x] ~~**The worker fetches its assignments once, at startup.**~~ Fixed:
      `IngestSettings.assignment_refresh` (default on) re-reads assignments on
      every heartbeat tick via `IngestWorker._reconcile_assignments`, which
      diffs the running set, requests stops for removed cameras and starts
      pumps for added ones — without tearing a capture handle out from under a
      blocked `read()`.
- [x] ~~**`PRAHARI_DETECT_*` is set by the chart and read by nothing yet.**~~
      `DetectorSettings` (`env_prefix="PRAHARI_DETECT_"`) landed on Day 2 with
      the cascade, and `tests/test_detector_settings.py` asserts every name the
      chart sets maps to a real field.
- [ ] **`make gateway-secret` loads the whole `.env`** into the Secret, not just
      the three gateway keys. Harmless (only three are referenced) and it keeps
      the password off the command line and out of shell history — but narrow it
      if `.env` ever grows something that should not be a Secret.
- [ ] **Verify the Timescale+PostGIS image tag** and swap `postgres.image`, or
      decide the pruner is enough and delete the comment. Docker was not running
      when the chart was written, so no tag has been pulled. Do this before Day 4
      rather than during the cutover.

## Day 2 — 3 Sep · detection end-to-end (local, CPU/MPS)

- [x] Wire `SampleGate` → motion gate → `yolov8n` → plate crop → OCR.
- [x] Indian-plate normalisation: `SS-DD-LL-NNNN`, BH-series, military,
      non-conforming. Formats already modelled in `events.proto`.
- [x] `make proto` and wire the gRPC client (`MetadataIngestService`).
- [x] `services/match-engine/` — confusion-aware fuzzy matcher (`0/O/D`, `8/B`,
      `1/I/L`, `5/S`, `2/Z`, `6/G`), Bloom prefilter, dedup, alert fan-out.
      **Highest-ROI item in the build** — exact match fails the live test silently.
- [x] `data/watchlist/` — representative stolen / wanted / missing dataset.
- [x] Tamper + black-frame detection. Must not fire at `loop_epoch` changes.
      The worker currently reports `black_frame_ratio: null` and
      `tamper_suspected: false` — null deliberately, so a console cannot show
      "no tampering detected" for a detector that is not running. Both become
      real values here.
- [x] `DetectorSettings` with `env_prefix="PRAHARI_DETECT_"`, matching the names
      the chart already sets (model, decode backend, batch size, motion gate,
      match-engine address).
- [x] **Gate:** known plate through a clip → detection → fuzzy match → alert in
      console in < 5 s.

## Day 3 — 4 Sep · route reconstruction + UI (local) — **GO/NO-GO**

Shipped, with one honest carve-out noted inline. The BFF shipped in its
ORG-TIERS form (sessions + org-tree scoping), which supersedes the static
`BFF_API_KEYS` shape `docs/DAY3-DESIGN.md §4.1` originally specified — see the
superseded note at the top of that section.

- [x] `services/correlation/` — cross-camera stitching, spatio-temporal
      feasibility gating (reject 200 km in 3 min), gap interpolation.
      `feasibility.py`, `bridging.py`, `store.py`, plus `test_feasibility.py`
      et al. Haversine + speed envelope; road-network routing stays a stated
      limitation.
- [x] `services/bff/` — auth (argon2id sessions + API keys), org-scoped RBAC,
      hash-chained audit log with actor + purpose code (denials logged too),
      SSE to the browser.
- [x] `web/` — Next.js 16 + MapLibre console: auth-gated board, camera health
      map, alert console on SSE, plate trace, onboarding + admin panels.
- [x] `web/` WHEP live preview — shipped as the *audited* path, not the raw
      `StreamEndpoints.whep_url` originally sketched: `endpoints` never reach
      the browser; the drawer mints a scoped, expiring ticket after the
      `video_preview` audit row (`docs/SECURITY.md`, `docs/EVIDENCE.md`).
- [x] **CSV/PDF report export.** Literally required: detected vehicles/plates
      with corresponding timestamps. `bff/export.py` behind
      `GET /api/v1/routes/{plate}/export?format=csv|pdf`, purpose-coded and
      audited.
- [x] **Gate:** the full vertical slice runs on the laptop. This decides whether
      any GPU money gets spent. `tests/test_day3_gate.py` — plate in,
      timestamped route out, an injected ~900 km/5 min hop rejected, a
      non-watchlist plate correctly producing no alert.

## Org tiers — opened after Day 3 (design: `docs/ORG-TIERS-DESIGN.md`)

- [x] Stage 1 — org tree (`ltree`) + required scope predicate in the registry
      (migration `005_orgs.sql`).
- [x] Stage 2 — identity: `users`/`sessions`/`api_keys` in Postgres
      (migration `006_identity.sql`), argon2id passwords, login/logout/me.
- [x] Stage 3 — BFF scoped surface: org-forced camera/gap proxy, purpose codes,
      hash-chained audit (denials logged), SSE alert relay, plate→route +
      CSV/PDF export.
- [x] Stage 4a–4d — `cameras.adapter`, AES-GCM stream credentials
      (`PRAHARI_CREDENTIAL_KEY`), org-scoped camera writes, SSRF-hardened RTSP
      probe, bulk CSV import.
- [x] Stage 5 — the Next.js console (auth gate, scoped API client, panels).
- [ ] Stage 4e/5e — on-prem ONVIF discovery agent. Only the `onvif` enum labels
      exist; no agent code. Highest-risk item in the design, severable by
      design.
- [x] Stage 6 — Helm wiring for the new services: `bffEnv`/`correlationEnv`
      blocks, `PRAHARI_INTERNAL_TOKEN` + `PRAHARI_CREDENTIAL_KEY` from the
      out-of-band `prahari-internal` Secret (per-service tokens and a
      dedicated `worker-token` on top), audit `audit.db` PVC, Dockerfiles for
      all six services, NetworkPolicies per flow.
- [x] `tests/test_org_tiers_gate.py` — in the tree.

## Day 4 — 5 Sep · cloud cutover

- [ ] `terraform apply` `envs/demo` (module is written and `validate`-clean;
      **not yet applied**).
- [ ] `helm upgrade --set profile=gpu`. If this needs a *code* change, the
      switch is wrong — fix the switch.
- [ ] **[est]** Measure streams-per-GPU. Replace `streams_per_gpu = 50` in the
      Terraform module and `values-gpu.yaml`'s `maxActiveCameras: 50`.
- [ ] Record the run in `infra/loadtest/`. A figure that can't be reproduced
      on demand does not ship.

## Day 5 — 6 Sep · scale + documents

- [ ] Load test to 200–500 virtual cameras; **film KEDA scaling 2 → 20 pods.**
- [ ] `docs/SCALE-80K.md` — every number traced to a recorded run. (Exists,
      honestly labelled; loadtest runs still pending a cluster.)
- [x] `docs/COST-MODEL.md`, `docs/SECURITY.md`, `docs/HLD.md`,
      `docs/DEMO-SCRIPT.md`, `docs/OPERATIONS.md`, `docs/EVIDENCE.md`,
      `docs/OBSERVABILITY.md`, `docs/KEYCLOAK.md` — all in the tree.
- [ ] PPT.

## Day 6 — 7 Sep · submit

- [ ] Record both videos (own-feed 2–3 min; government-feed live).
- [ ] **Dry-run "here's a plate, trace it" at least five times.**
- [x] `docs/DEMO-SCRIPT.md` — written: six beats, each with click-path →
      what it proves → the invariant it demonstrates.
- [ ] Submit well before the deadline. Not at it.

---

## Verification checklist (run before submitting)

From the integrator's guide §4 plus our own invariants. Tick only what has been
*observed*, not what looks correct in the source.

- [x] Every client forces RTSP over TCP — structural via `rtsp_env.py`; a bare
      `import cv2` is rejected by ruff TID251 (verified).
- [x] No timing logic uses `CAP_PROP_FPS` or frame arrival time (11 tests).
- [x] Inter-frame gaps do not crash or stall the pipeline (tested).
- [x] Decoder warnings at join are logged, not fatal (grace window).
- [ ] Reconnect with backoff **tested by actually restarting a feed** — the code
      is written and unit-tested; it has never met a real disconnect.
- [ ] Camera list and per-camera properties read from `/api/ingest` — code
      written, never run against the live endpoint.
- [ ] Mixed H.264/H.265 and mixed resolutions handled end-to-end.
- [ ] Behaviour sane across a real scene discontinuity (two full loop cycles,
      tamper detector silent).
- [ ] Kill an inference pod mid-run: rescheduled, stream reconnects, no events
      lost.
- [ ] Kill an upstream stream: worker backs off, registry flips the camera
      unhealthy, pipeline survives.
- [ ] Registry has run against a real Postgres: PostGIS extension created,
      migrations applied under the advisory lock, `camera_current` staleness
      overlay correct against a real `now()`. **Partially met** — the verified
      quick local run exercised real Postgres + real migrations; the staleness
      overlay against a real `now()` still has no observed heartbeat-driven
      confirmation.
- [ ] A camera observed going healthy → stale → healthy without a gateway,
      driven by hand-posted heartbeats.
- [ ] MediaMTX reconciliation observed adding and removing a real path, and
      observed skipping (not failing) when the restreamer is down.
- [ ] Full submission dry-run scored against the Step 7 rubric by the
      `submission-producer` agent acting as jury.

## Carrying unverified numbers

These must not reach a slide in their current state:

| Figure | Where | Status |
|---|---|---|
| 50 streams/GPU | `modules/district/variables.tf`, `values-gpu.yaml` | **estimate** — measure Day 4 |
| Timescale+PostGIS image tag | `values.yaml` `postgres.image` comment | **unverified** — never pulled; `postgis/postgis:16-3.4` is the tested default |
| ~1,600 GPUs statewide | derived from the above | follows automatically |
| 160 Gbps / 52 PB | `README.md`, `CLAUDE.md` | arithmetic from the brief — sound |
| < 250 Mbps / ~6 TB/mo | same | **estimate** — validate against real event rates |
