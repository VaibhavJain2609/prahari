# TODO

**Deadline: 7 Sep 2026.** Today is 2 Sep. Event 10–11 Sep.
Reasoning behind the ordering is in `docs/PLAN.md`; this file is the checklist.

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
      yet. **Not met** — needs the console (Day 3) and a live catalogue.

## Before Day 2 starts

Day 1 is code-complete and has never touched a database. These are the things
that will actually go wrong:

- [ ] `make cluster && make images && make up` — first real run. Postgres cold
      start vs. the registry's startup probe, PostGIS extension creation, the
      migration advisory lock.
- [ ] Confirm `camera_current` returns `effective_health_state` correctly with a
      real `now()` — the staleness overlay is pure SQL and has no unit test.
- [ ] Register one camera by hand (`POST /api/v1/cameras`), heartbeat it, watch
      it go healthy then stale. This exercises the whole health path without the
      gateway.
- [ ] Kill the registry pod mid-heartbeat: workers must keep pulling and recover
      on the next report.

### Gaps Day 1 opened and did not close

- [ ] **`PRAHARI_INGEST_USE_HLS` is documented in `.env.example` and read by
      nothing.** `IngestSettings` has no `use_hls` field, so the documented HLS
      fallback cannot actually be switched on. Either wire it through to
      `StreamCapture(use_hls=...)` or delete the line — a fallback that only
      exists in a comment is worse than none, because it will be trusted at the
      venue when 8554 turns out to be blocked.
- [ ] **The worker fetches its assignments once, at startup.** Cameras the
      catalogue sync adds later are never picked up until the pod restarts. Fine
      for a 5-stream laptop demo; wrong the moment the estate changes during a
      run. Re-fetch on the heartbeat interval, and diff against the running set.
- [ ] **`PRAHARI_DETECT_*` is set by the chart and read by nothing yet.** The
      settings class lands with detection on Day 2. If Day 2 slips, delete the
      env block rather than leaving dead knobs that look like working ones.
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

- [ ] `services/correlation/` — cross-camera stitching, spatio-temporal
      feasibility gating (reject 200 km in 3 min), gap interpolation.
- [ ] `services/bff/` — auth, RBAC, hash-chained audit log with actor +
      purpose code, SSE to the browser.
- [ ] `web/` — Next.js + MapLibre, alert console, WHEP live preview
      (preview only — never an inference source).
- [ ] **CSV/PDF report export.** Literally required: detected vehicles/plates
      with corresponding timestamps.
- [ ] **Gate:** the full vertical slice runs on the laptop. This decides whether
      any GPU money gets spent.

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
- [ ] `docs/SCALE-80K.md` — every number traced to a recorded run.
- [ ] `docs/COST-MODEL.md`, `docs/SECURITY.md` (DPDP Act 2023), `docs/HLD.md`.
- [ ] PPT.

## Day 6 — 7 Sep · submit

- [ ] Record both videos (own-feed 2–3 min; government-feed live).
- [ ] **Dry-run "here's a plate, trace it" at least five times.**
- [ ] `docs/DEMO-SCRIPT.md`.
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
      overlay correct against a real `now()`. All of it is currently tested
      against fakes only.
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
