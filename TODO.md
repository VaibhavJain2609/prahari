# TODO

**Deadline: 7 Sep 2026.** Today is 1 Sep. Event 10–11 Sep.
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

- [ ] `services/registry/` — FastAPI + PostGIS. Camera CRUD, `/healthz`
      (the Helm probes already expect it).
- [ ] PostGIS schema + migration; TimescaleDB extension for the detection
      time-series.
- [ ] Catalogue sync: `/api/ingest` → registry, idempotent, re-runnable.
      Ids change; sync must handle cameras appearing and disappearing.
- [ ] Populate MediaMTX paths from the registry at runtime. The ConfigMap ships
      with `paths:` deliberately empty — a hardcoded path passes locally and
      fails on demo day.
- [ ] Camera health: heartbeat + `measured_fps` drift (never `CAP_PROP_FPS`),
      feeding Model 1 gap analysis.
- [ ] `Tiltfile` — inner dev loop against k3d.
- [ ] **Gate:** every camera visible on the map with live health. None analysed yet.

## Day 2 — 3 Sep · detection end-to-end (local, CPU/MPS)

- [ ] Wire `SampleGate` → motion gate → `yolov8n` → plate crop → OCR.
- [ ] Indian-plate normalisation: `SS-DD-LL-NNNN`, BH-series, military,
      non-conforming. Formats already modelled in `events.proto`.
- [ ] `make proto` and wire the gRPC client (`MetadataIngestService`).
- [ ] `services/match-engine/` — confusion-aware fuzzy matcher (`0/O/D`, `8/B`,
      `1/I/L`, `5/S`, `2/Z`, `6/G`), Bloom prefilter, dedup, alert fan-out.
      **Highest-ROI item in the build** — exact match fails the live test silently.
- [ ] `data/watchlist/` — representative stolen / wanted / missing dataset.
- [ ] Tamper + black-frame detection. Must not fire at `loop_epoch` changes.
- [ ] **Gate:** known plate through a clip → detection → fuzzy match → alert in
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
- [ ] Full submission dry-run scored against the Step 7 rubric by the
      `submission-producer` agent acting as jury.

## Carrying unverified numbers

These must not reach a slide in their current state:

| Figure | Where | Status |
|---|---|---|
| 50 streams/GPU | `modules/district/variables.tf`, `values-gpu.yaml` | **estimate** — measure Day 4 |
| ~1,600 GPUs statewide | derived from the above | follows automatically |
| 160 Gbps / 52 PB | `README.md`, `CLAUDE.md` | arithmetic from the brief — sound |
| < 250 Mbps / ~6 TB/mo | same | **estimate** — validate against real event rates |
