# PRAHARI — Gujarat Sentinel Hackathon 2026

Statewide CCTV integration + video-analytics platform for the Gujarat Police Hackathon
Innovation Challenge 2026 (`sentinel.gujarat.gov.in`, State Crime Records Bureau).

**Submission deadline: 7 Sep 2026.** Event 10–11 Sep. Category 1 (student/startup).
Full plan: `docs/PLAN.md`.

---

## The one idea everything derives from

You cannot centralise 80,000 video streams.

| | Centralise pixels | Centralise metadata (ours) |
|---|---|---|
| Backhaul | 80,000 × 2 Mbps = **160 Gbps** | **< 250 Mbps** |
| 30-day storage | **~52 PB** | **~6 TB/month** |

So the system is **three planes**:

- **Control plane** — camera registry + GIS. Small, authoritative. (Reference Model 1, mandatory.)
- **Data plane** — video **stays at the edge**. Pulled centrally only as audited evidence.
- **Metadata plane** — detections, plates, tracks, alerts flow centrally as protobuf events.

We implement **Model 1 + Model 3** (registry/GIS + federation middleware), with Model 2
direct-connect as one adapter class, and we explicitly reject Model 4 with the numbers above.

When a design decision is ambiguous, pick the option that keeps pixels at the edge.

---

## Hard invariants

These come from the portal's own Integrator's Guide. Violating them costs hours of debugging.

**Stream handling**
- **Resolve cameras via the `/api/ingest` catalogue, always.** "The catalogue is the contract,
  the URL pattern is not." Never hardcode a `/stream/<id>` URL — ids and camera sets change.
- **Force RTSP over TCP** (`rtsp_transport=tcp`). Fall back to HLS if port 8554 is blocked.
- **Never trust `CAP_PROP_FPS`.** Drive all timing from PTS / `CAP_PROP_POS_MSEC`.
  Cadence is not constant and the reported rate lies.
- **Feeds are live-only.** No byte-range fetch, no `curl`/`wget` of `/stream/<id>` — you get a
  partial file that *looks* complete and will silently corrupt any analysis built on it.
- **Feeds loop, producing an abrupt scene cut.** The tamper / black-frame detector **must**
  whitelist loop points or it fires on every camera, every cycle.
- **Reconnect with exponential backoff**, ~2 s start, ~30 s cap. Decoder warnings when joining
  mid-stream are expected and self-correcting — do not treat them as errors.
- **Consume only.** Never publish to the gateway, never call its control API.
- **One upstream pull per camera.** Every client gets its own copy of the stream, so all
  consumers fan out from MediaMTX. Connect only to cameras actually in use.

**Contracts**
- All inter-service messages are protobuf, defined in `proto/`. Regenerate stubs with
  `make proto`; never hand-edit generated code.
- gRPC for the high-rate worker→match-engine link and the `CameraAdapter` plugin ABI.
  REST/JSON + SSE for anything the browser touches.
- The same protobuf messages go on the bus. One schema, two transports.

**Deployment**
- **Helm + Terraform in `infra/` are the only source of truth.** No `docker-compose.yml`, no
  `kubectl apply` of ad-hoc YAML, no clicking in a cloud console. If it isn't in `infra/`,
  it does not exist. Anything in the demo must survive a clean `terraform apply`.
- Every service must tolerate being killed and rescheduled at any moment.
  No local disk state outside a PVC.
- The `profile` Helm value (`local` | `gpu`) is the **only** thing that differs between the
  laptop and the cloud. If a cutover needs a code change, the switch is wrong — fix the switch.

**Privacy & evidence**
- Video never leaves the edge except as an explicit, audited evidence request.
- Every video access is written to the hash-chained audit log, with an actor and a purpose code.
  No exceptions, no "internal" bypass path.
- Face-derived data is handled under a stricter gate than plate data. Keep them separable.

---

## Layout

```
proto/prahari/v1/     the vendor-neutral contract — start here
services/
  registry/           FastAPI + PostGIS · catalogue sync · gap analysis · camera health
  inference/          YOLO + OCR workers · decode · motion gating · batching · gRPC client
  match-engine/       fuzzy watchlist match · dedup · alert fan-out
  correlation/        cross-camera track stitching · feasibility gating · route reconstruction
  bff/                auth · RBAC · hash-chained audit · SSE to the browser
web/                  Next.js · MapLibre · WHEP live preview · alert console
infra/
  terraform/modules/district/   reusable per-district unit (statewide rollout artifact)
  helm/prahari/                 one chart, profile-switched
  k3d/                          local cluster
docs/                 HLD · SCALE-80K · SECURITY · COST-MODEL · DEMO-SCRIPT
data/catalogue/       captured /api/ingest snapshots
data/watchlist/       representative stolen / wanted / missing dataset
```

## Stack

Python **3.12** (pinned — 3.14 has no PyTorch/Ultralytics wheels) · FastAPI · grpcio ·
Ultralytics YOLO · PaddleOCR · Postgres 16 + PostGIS + TimescaleDB · Redis ·
Redpanda (scale test) · Next.js 15 + MapLibre GL · k3s/k3d + Helm + Terraform + KEDA.

## Commands

```
make proto        regenerate protobuf stubs
make up           k3d cluster + helm install, profile=local
make dev          tilt up (inner loop)
make test         pytest across services
make down         tear down the local cluster
```

---

## Working agreements

- **Local first.** Days 0–3 run entirely on k3d on the laptop, CPU/MPS, 3–5 streams.
  The GPU is rented once, on Day 4, for measurement and the demo. Do not reach for cloud
  to solve a problem that is reproducible locally.
- **Measure, don't assert.** Every scalability number in `docs/SCALE-80K.md` must trace to a
  recorded run in `infra/loadtest/`. A figure we cannot reproduce on demand does not ship.
- **The mandatory test case outranks everything.** Onboard the government feed, take a
  registration number, return a timestamped location-wise route. Bonus features explicitly do
  not compensate for failing it. When time is short, cut breadth, never that path.
- Mock-ups, animations and simulated interfaces are disqualifying. If it is in a demo video,
  it is running code.
