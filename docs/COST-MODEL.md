# Cost model

The cost argument the architecture rests on: the platform is cheap *because*
video never moves. Every number below is either arithmetic from a stated
assumption or a measured run in `infra/loadtest/runs/` — and `runs/` is
currently empty, so today every figure is an **estimate** and says so. That
is the same honesty standard `docs/SCALE-80K.md` holds itself to; this doc
adds the money-shaped view of the same arithmetic.

## 1. What centralising pixels would cost

From `SCALE-80K.md` §1 — arithmetic, **estimate**, deliberately not measured
because the point is that it is orders of magnitude too large:

| Quantity | Derivation | Value |
|---|---|---|
| Backhaul | 80,000 cams × 2 Mbps | **160 Gbps** |
| 30-day storage | 160 Gbps × 2.592×10⁶ s ÷ 8 | **~52 PB** |

160 Gbps of sustained ingest is tier-1-ISP transit, and ~52 PB of retained
video is a storage business, not a feature. No cloud-instance pricing is
attached because the number is disqualifying before pricing matters — that
is the rejection of Reference Model 4.

## 2. What the metadata plane costs

From `SCALE-80K.md` §2 — **estimate**, assumption arithmetic at stated
per-camera rates:

| Flow | Volume |
|---|---|
| Heartbeats (8,000 msg/s × ~200 B) | ~13 Mbps |
| Detections (8,000 msg/s × ~500 B protobuf) | ~32 Mbps |
| Alerts (8–80 msg/s × ~1 KB) | < 1 Mbps |
| Operator reads | < 10 Mbps |
| **Total** | **< 60 Mbps** — ~4× headroom under the < 250 Mbps claim |

30-day metadata storage: **~6–10 TB/month** (the spread is the detection-rate
assumption, 0.05–0.1 det/s/camera — flagged in `SCALE-80K.md` §2, not
smoothed). Sensitivity lives in the assumptions; even a busy-junction rate of
0.5 det/s/camera lands ~160 Mbps, still inside the bound.

The centralised estate this buys: one Postgres+PostGIS, one Redis, and a
handful of stateless replicas — commodity nodes, not exotic capacity. The
places 80k actually strains are named in `SCALE-80K.md` §5 (Postgres
heartbeat writes, the bus), and they are scaling problems, not cost cliffs.

## 3. Compute: the number that actually moves money

The expensive resource is inference, and it is distributed at the edge per
district — which is the point of the architecture: you buy GPU where the
cameras already are, not bandwidth to a centre.

- `streams_per_gpu = 50` — **unverified estimate** (L4 class, yolov8s,
  4 fps sampled, OCR gated on detections), declared as such in
  `infra/terraform/modules/district/variables.tf` and `values-gpu.yaml`.
  Also a CPU-decode bound until NVDEC is real (`SCALE-80K.md` §4).
- District sizing derives as `ceil(camera_count / streams_per_gpu)`
  (`modules/district/main.tf`) — the deployment arithmetic and the capacity
  claim cannot drift apart because they are the same expression.
- Worked example: `camera_count=4200` → **84 GPU nodes** (`g6.xlarge`-class,
  the module default). The statewide figure — 80,000 / 50 = **1,600 GPUs**
  at full concurrency — is the number the same arithmetic produces and the
  number to pressure-test when the measurement exists.

If the measured knee comes in at 25 streams/GPU, every figure above doubles
and the architecture still wins — the pixel column moves by zero. That
asymmetry *is* the cost model: our uncertainty sits in node counts, the
rejected design's certainty sits at 160 Gbps.

## 4. What the hackathon build itself costs

From `docs/PLAN.md` §5, stated budget figures:

- Total cloud budget ~**$200–300** in credits; the GPU is rented once, for
  ~20–25 GPU-hours ≈ **~$20**, on Day 4 — after the Day-3 gate proves the
  vertical slice on a laptop.
- Managed control planes rejected on cost: EKS/GKE ≈ **$73/mo each** before
  a single GPU runs; k3s/k3d keeps the entire spend on the thing being
  measured.

## 5. What is not yet priced

Honest edges of this model:

- **Edge hardware at existing sites** — cameras, DVRs and district nodes are
  assumed present; the model prices the *platform*, not estate build-out.
- **Egress for audited evidence pulls** — the pull path is designed, not
  built (`NEXT-PHASE-PLAN.md` §2/S4); its cost is per-request and bounded by
  audit, but unmeasured.
- **Retention beyond 30 days of metadata** and Postgres at estate scale —
  named gaps in `SCALE-80K.md` §5, awaiting a measured run.
