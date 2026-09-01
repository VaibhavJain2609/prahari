# PRAHARI

**Unified camera intelligence for Gujarat — without moving the video.**

Submission for the Gujarat Police Hackathon Innovation Challenge 2026 (State Crime
Records Bureau), Category 1. Working name; प्रहरी = *sentinel*.

---

## The problem, stated as arithmetic

The challenge asks for one platform over **~80,000 cameras, 26 departments,
34 districts** — mixed analog and IP, several VMS vendors, cloud and local
storage, 7–15 day retention.

The obvious design is a central VMS that all cameras stream into. It does not
survive contact with a calculator:

| | Centralise pixels | Centralise metadata |
|---|---|---|
| Backhaul | 80,000 × 2 Mbps = **160 Gbps** | **< 250 Mbps** |
| 30-day storage | **~52 PB** | **~6 TB/month** |

Roughly **650× less bandwidth** and **~8,600× less storage**. That gap is the
whole architecture, and it is why this submission explicitly *rejects* the
challenge's Reference Model 4 (Central VMS) and builds a hybrid of **Model 1**
(registry + GIS, mandatory for every entry) and **Model 3** (federation
middleware), with Model 2 direct-connect as one adapter class.

## Three planes

- **Control plane** — camera registry + GIS. Small, authoritative, live.
- **Data plane** — video **stays at the edge**. Pulled centrally only on
  explicit, audited evidence requests.
- **Metadata plane** — detections, plate reads, tracks and alerts flow centrally
  as protobuf events.

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
   ┌────────▼────────┐  confusion-aware fuzzy plate match · dedup
   │  match-engine   │
   └────────┬────────┘
            │ same protobuf on the bus (Redis Streams / Redpanda)
   ┌────────┼────────┬──────────────┐
   ▼        ▼        ▼              ▼
registry  correlation  BFF ──── Next.js console
+ PostGIS  route recon  REST+SSE   MapLibre · WHEP preview
```

## What is deliberately different

1. **Confusion-aware fuzzy plate matching.** Indian-plate OCR reliably confuses
   `0/O/D`, `8/B`, `1/I/L`, `5/S`, `2/Z`, `6/G`. Exact-match watchlists fail the
   live "trace this registration number" test *silently*. Weighted edit distance
   over a confusion matrix, plus `SS-DD-LL-NNNN` normalisation including
   BH-series.
2. **Spatio-temporal feasibility gating** on route reconstruction — reject
   impossible hops (200 km in 3 minutes) rather than plotting them.
3. **Hash-chained audit log**, per-department RBAC, purpose codes, DPDP Act 2023
   alignment.
4. **Zero-code onboarding** — one sync pulls the whole `/api/ingest` catalogue
   into the registry.
5. **Live camera health** — heartbeat, FPS drift, black-frame and tamper
   detection — feeding Model 1's coverage-gap analysis.
6. **A measured scaling curve**, on rented GPU hardware, rather than an asserted
   one.

## Repository layout

```
proto/          the vendor-neutral contract (buf-linted, STANDARD)
services/       registry · inference · match-engine · correlation · bff
web/            Next.js console
infra/
  helm/         umbrella chart; profile: local | gpu is the ONLY cutover knob
  k3d/          local cluster — same Kubernetes API as the cloud
  terraform/    modules/district — statewide rollout as a runnable artifact
docs/           HLD · SCALE-80K · SECURITY · COST-MODEL · DEMO-SCRIPT
```

## Local first

Everything is built and debugged on `k3d` on a laptop before a single GPU hour
is spent.

```bash
make cluster          # k3d cluster + local registry
make proto            # generate protobuf stubs
make up               # helm upgrade --install, profile: local
make verify           # render both profiles and diff them
```

`make up PROFILE=gpu` is the entire cloud cutover. The `profile` value swaps
model size, decode backend (VideoToolbox → NVDEC), sampling rate, stream cap,
batch size, GPU resource requests and KEDA autoscaling — all as *values*, never
as code paths.

> **The invariant:** if a cutover ever needs a code change, the switch is wrong.
> Fix the switch, not the code.

k3d on macOS has no GPU passthrough, so local runs are CPU/MPS with `yolov8n` at
2 fps over ~5 streams. That proves pipeline **correctness**; it cannot produce
the scaling curve. The curve comes from Day 4 on real hardware.

## Statewide rollout

```bash
terraform apply -var district=rajkot -var camera_count=4200
```

The `district` module derives GPU node count as
`ceil(camera_count / streams_per_gpu)` from the *measured* streams-per-GPU
figure, so the deployment arithmetic and the capacity claim in `docs/SCALE-80K.md`
cannot drift apart. Nothing district-specific is hardcoded in the module body.

## Status

Day 0 scaffold. Protobuf contract, Helm chart with a verified profile switch,
k3d cluster config and the Terraform district module are in place; services are
not yet implemented. See `CLAUDE.md` for the hard invariants — several of them
(RTSP over TCP, never trust `CAP_PROP_FPS`, feeds loop) come straight from the
portal's Integrator's Guide and will otherwise cost real debugging hours.
