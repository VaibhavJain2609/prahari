# PRAHARI — build plan

Referenced from `CLAUDE.md`. This is the *why*; `TODO.md` is the *what next*.

**Submission 7 Sep 2026** (registration closes the same day) · event 10–11 Sep ·
Category 1 (student/startup) · budget ~$200–300 in cloud credits.

---

## 1. The problem, and why the obvious answer fails

The challenge asks for one platform over **~80,000 cameras, 26 departments,
34 districts** — mixed analog and IP, several VMS vendors, cloud and local
storage, 7–15 day retention.

The instinctive design is a central VMS that every camera streams into. It does
not survive a calculator:

| | Centralise pixels | Centralise metadata |
|---|---|---|
| Backhaul | 80,000 × 2 Mbps = **160 Gbps** | **< 250 Mbps** |
| 30-day storage | **~52 PB** | **~6 TB/month** |

≈ **650× less bandwidth**, ≈ **8,600× less storage**.

This single comparison is the spine of the submission. Every other decision —
cost, DR, GPU sizing, rollout, privacy posture — falls out of it. We therefore
**explicitly reject Reference Model 4 (Central VMS)** and say so with the
numbers, because arguing the rejection is more persuasive than quietly not
implementing it.

**Chosen: Model 1 + Model 3.** Registry/GIS (mandatory for every entry) plus
federation middleware, with Model 2 direct-connect as one adapter class.

## 2. Three planes

- **Control plane** — camera registry + GIS. Small, authoritative, live.
- **Data plane** — video **stays at the edge**. Pulled centrally only as an
  explicit, audited evidence request.
- **Metadata plane** — detections, plate reads, tracks, alerts flow centrally as
  protobuf events.

> When a design decision is ambiguous, pick the option that keeps pixels at the
> edge.

## 3. Six things most entries will not do

1. **Confusion-aware fuzzy plate matching.** Indian-plate OCR reliably confuses
   `0/O/D`, `8/B`, `1/I/L`, `5/S`, `2/Z`, `6/G`. An exact-match watchlist fails
   the live "trace this registration number" test **silently** — it returns
   nothing and looks like the vehicle was never there. Weighted edit distance
   over a confusion matrix, plus format normalisation. Highest ROI in the plan.
2. **Spatio-temporal feasibility gating** on routes — reject impossible hops
   (200 km in 3 minutes) instead of plotting them. Produces a defensible route
   rather than a scatter of hits.
3. **Hash-chained audit log**, per-department RBAC, purpose codes, DPDP Act 2023
   alignment. Government juries weight this heavily; hackathon teams skip it.
4. **Zero-code onboarding** — one sync ingests the whole `/api/ingest`
   catalogue. A 30-second demo beat answering "integrate heterogeneous cameras".
5. **Live camera health** — heartbeat, measured-FPS drift, black-frame and
   tamper detection — so the registry is live, not a spreadsheet.
6. **A measured scaling curve** on real hardware, not an asserted one.

## 4. Architecture

```
 govt feeds (RTSP/HLS/WHEP)
            │
   ┌────────▼─────────────── MediaMTX ───────────────┐
   │   one upstream pull per camera, fanned out      │
   └────────┬────────────────────────────────────────┘
            │ RTSP (TCP)
   ┌────────▼────────┐  decode → motion gate → YOLO → crop → OCR
   │ inference pool  │  NVDEC/VideoToolbox · cross-camera batching
   └────────┬────────┘  KEDA-scaled on queue depth
            │ gRPC client-stream (protobuf)
   ┌────────▼────────┐  confusion-aware fuzzy match · dedup
   │  match-engine   │
   └────────┬────────┘
            │ same protobuf on the bus (Redis Streams / Redpanda)
   ┌────────┼────────┬──────────────┐
   ▼        ▼        ▼              ▼
registry  correlation  BFF ──── Next.js console
+ PostGIS  route recon  REST+SSE   MapLibre · WHEP preview
```

**Transport split.** gRPC + protobuf on the worker→match-engine link (millions
of small messages, one multiplexed HTTP/2 connection per worker) and as the
`CameraAdapterService` ABI — any vendor implements the `.proto` in any language
and drops in. REST/JSON + SSE for the browser. The *same* protobuf goes on the
bus, so one schema serves both transports and we keep replay and backpressure.

gRPC does **not** make inference faster. It earns its place on the high-rate
link and as the vendor-neutrality artifact. Claiming otherwise would not survive
a technical judge.

**MediaMTX, not a hand-rolled gateway.** Each client receives its own copy of
the stream, so N consumers × M cameras is untenable. MediaMTX pulls each camera
once and fans out. Saves a build day.

## 5. Local first

Days 0–3 run entirely on k3d on the laptop. The GPU is rented once, on Day 4,
for measurement and the demo — roughly 20–25 GPU-hours, ~$20 of $250. Managed
control planes were rejected: EKS/GKE cost ~$73/mo *each* before a single GPU
runs.

k3d on macOS has **no GPU passthrough**. Local inference is CPU/MPS with
`yolov8n` at 2 fps over ~5 streams. That proves pipeline **correctness** and
cannot produce the scaling curve. Hence:

> The `profile` Helm value (`local` | `gpu`) is the **only** thing that differs
> between laptop and cloud. If a cutover needs a code change, the switch is
> wrong — fix the switch.

Verified by diffing rendered manifests: model, decode backend, sampling rate,
stream cap and batch size all swap; KEDA `ScaledObject`, `nvidia.com/gpu`
requests and `runtimeClassName` appear only under `gpu`.

## 6. Statewide rollout as a runnable artifact

`infra/terraform/modules/district/` turns "how would you deploy to 34
districts?" from a paragraph in a deck into `terraform apply -var
district=rajkot -var camera_count=4200`. Node count derives as
`ceil(camera_count / streams_per_gpu)`, so the deployment arithmetic and the
capacity claim in `SCALE-80K.md` are the same number and cannot drift apart.

Written and `terraform validate`-clean. **Not applied** — that happens Day 4.

## 7. Schedule

| Day | Date | Focus | Gate |
|---|---|---|---|
| 0 | 1 Sep | Scaffold, contracts, infra, ingest layer | **done** |
| 1 | 2 Sep | Registry + PostGIS + GIS + catalogue sync + health | all cameras visible, health-tracked |
| 2 | 3 Sep | Detection → OCR → gRPC → match → alert | plate → alert < 5 s |
| 3 | 4 Sep | Correlation, BFF, console, report export | **vertical slice works on a laptop — go/no-go for cloud** |
| 4 | 5 Sep | `terraform apply`, `profile=gpu`, **measure** | real streams-per-GPU recorded |
| 5 | 6 Sep | Load test, KEDA on video, docs, PPT | curve reproducible on demand |
| 6 | 7 Sep | Videos, five dry-runs, submit | submitted early |

**The mandatory test case outranks everything**: onboard the government feed,
take a registration number, return a timestamped location-wise route. Bonus
features do not compensate for failing it. When time is short, cut breadth,
never that path.

## 8. What is already built (1 Sep)

- `proto/prahari/v1/` — vendor-neutral contract, `buf lint` STANDARD clean.
  `char_confidence` survives to the match engine; `loop_epoch` /
  `loop_boundary` encode the looping-feed hazard.
- `infra/helm/prahari/` — umbrella chart, profile switch verified by diff.
- `infra/k3d/` — local cluster, same Kubernetes API as the cloud node.
- `infra/terraform/modules/district/` + `envs/demo` — validate-clean, unapplied.
- `services/inference/` — ingest layer: PTS-driven timing, GOP-replay-burst
  detection, loop-cut epochs, supervised reconnect with jittered backoff,
  alias-tolerant catalogue client. 11 tests, no network required.
- `.claude/agents/` — nine domain agents; `settings.json` gates `terraform
  apply`, `helm install` and `git push` behind confirmation.

## 9. Known risks

| Risk | Mitigation |
|---|---|
| **Registration closes 7 Sep** with submission | Register Day 0. Everything else is downstream. |
| Catalogue JSON keys unverified | Alias-tolerant parsing + `raw` kept; tighten on first snapshot. |
| Password mechanism unverified | Both plausible forms sent; delete the wrong one on first 200. |
| 50 streams/GPU is an estimate | Measured Day 4; the Terraform arithmetic updates with it. |
| Port 8554 blocked at the venue | HLS fallback implemented — **must be tested, not assumed.** |
| Reconnect never met a real disconnect | Day 1: restart a feed and watch it recover. |
| Loop cut fires the tamper detector | `loop_epoch` in the contract; a scene cut alone is never an alert — tamper requires a *sustained* anomaly, which a loop does not produce. |
| GPU spend before the slice works | Day 3 is an explicit go/no-go. |
| Mock-ups are disqualifying | If it is in the video, it is running code. No exceptions. |

## 10. Open questions

- Team size, registration fee, IP terms, finale hardware — unstated on the site.
- "PRAHARI" (प्रहरी, *sentinel*) is a placeholder. Rename freely.
