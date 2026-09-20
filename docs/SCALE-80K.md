# SCALE-80K — the 80,000-camera scaling case, measured

The numbers behind "you cannot centralise 80,000 video streams" — and the
numbers behind why centralising *metadata* works instead.

**The rule (CLAUDE.md):** every figure in this doc traces to either (a)
arithmetic from stated assumptions, or (b) a recorded run in
`infra/loadtest/runs/`. A cell marked **estimate** or **unmeasured** is
exactly that — nothing below pretends otherwise. When a run produces a real
number, the cell is updated and the run directory is cited.

## 1. The pixel case (why Model 4 is rejected)

Arithmetic, not measurement — the point of these numbers is that they are
orders of magnitude too large to need measuring:

| Quantity | Derivation | Value |
|---|---|---|
| Camera count | challenge estate | ~80,000 |
| Per-stream bitrate | assumption: 2 Mbps avg (H.264/H.265, mixed 720p–1080p) | 2 Mbps |
| Centralised backhaul | 80,000 × 2 Mbps | **160 Gbps** |
| 30-day storage | 160 Gbps × 2.592×10⁶ s ÷ 8 | **~52 PB** |

A single 160 Gbps ingest is a tier-1 ISP problem, not a police IT budget.
Rejected, with the arithmetic on the table.

## 2. The metadata case (what we actually centralise)

Metadata-plane volumes, from stated per-camera rates. These are *assumption
arithmetic* until the loadtest numbers in §4 replace the per-message costs
with measured ones.

| Flow | Rate assumption | Message size assumption | Volume |
|---|---|---|---|
| Heartbeats | 80,000 cams × 1 / 10 s = 8,000 msg/s | ~200 B JSON | ~1.6 MB/s ≈ **13 Mbps** |
| Detections | 80,000 cams × ~0.1 det/s avg (motion-gated, 2 fps sampled) = 8,000 msg/s | ~500 B protobuf (incl. plate + embedding) | ~4 MB/s ≈ **32 Mbps** |
| Alerts | ~0.1–1 % of detections = 8–80 msg/s | ~1 KB protobuf | < 1 Mbps |
| Operator reads (geojson, routes) | interactive, ~tens/s peak | varies | < 10 Mbps |
| **Total** | | | **< 60 Mbps**, comfortably inside the < 250 Mbps claim |

The < 250 Mbps figure in README/HLD therefore stands as an *upper bound* with
~4× headroom — even a 0.5 det/s/camera detection rate (a busy junction, all
day) lands ~160 Mbps. Sensitivity lives in the assumptions, not the
architecture.

30-day metadata storage at the same rates: 8,000 det/s × 500 B × 2.592×10⁶ s
≈ **10 TB/month** raw detection payload; the ~6 TB figure quoted in README
corresponds to ~0.05 det/s/camera or smaller retained fields — both
assumptions are flagged here rather than smoothed over, and the loadtest's
measured message sizes will pin the real number.

## 3. The measured-numbers table

Status legend: **measured** (run cited) · **estimate** (arithmetic/stated
assumption) · **unmeasured** (no data yet — the harness exists to produce it).

| Metric | Value | Status | Source / run |
|---|---|---|---|
| Pixel backhaul @80k | 160 Gbps | estimate | §1 arithmetic |
| Pixel storage @80k, 30d | ~52 PB | estimate | §1 arithmetic |
| Metadata backhaul @80k | < 60 Mbps (target < 250) | estimate | §2 arithmetic |
| Metadata storage @80k, 30d | ~6–10 TB/month | estimate | §2 arithmetic — rate assumptions unverified |
| `streams_per_gpu` (L4, yolov8s, 4 fps sampled) | 50 | **unverified estimate** | `infra/terraform/modules/district/variables.tf` — pending Day-4 measurement |
| Camera registration throughput | — | **unmeasured** | `api.camera_create_ms` in a run |
| Heartbeat ingest @ N cams | — | **unmeasured** | `heartbeat.post_ms` rate in a run |
| Detection ingest throughput (gRPC) | — | **unmeasured** | `detection.sent` vs acked in a run |
| Detection→alert-visible latency | — | **unmeasured** | `e2e.alert_latency_ms` (floored at poll interval) |
| Camera list / geojson latency @ N | — | **unmeasured** | `api.cameras_*_ms` in a run |
| Route-build latency | — | **unmeasured** | `api.route_build_ms` in a run |
| Per-service CPU/mem @ N | — | **unmeasured** | `resource.sample` rows in a run |
| Heartbeat write rate @80k | 8,000/s | estimate | §2 — needs Postgres-scale validation |

## 4. How a real number gets produced

```bash
# Platform up (k3d) + port-forwards, then:
cd infra/loadtest
./run.sh run --cameras 5,50,500 --duration-s 60          # simulate mode
./run.sh run --mode live --cameras 20 --streams-live 5   # real streams
```

Each run lands in `infra/loadtest/runs/<timestamp>/` as `config.json` +
`samples.jsonl` + `summary.md`. To promote a number into §3: copy the
`summary.md` (or its relevant rows) into a committable location, cite the
run directory and git sha, and flip the cell from **unmeasured** to
**measured**. Harness usage, modes and limitations: `infra/loadtest/README.md`.

### What each mode does and does not establish

- **`simulate`** establishes the metadata plane's throughput/latency curve:
  registry write + read latency under N cameras, heartbeat ingestion, gRPC
  detection ingest rate, detection→alert latency, route-build latency.
  It establishes **nothing** about decode or inference — no pixels move.
- **`live`** establishes the full pipeline on N real streams (ffmpeg →
  MediaMTX → registry-reconciled pull paths → workers → gRPC → alerts).
  On a laptop it is host-bound; the number that matters from it is
  *streams sustained per CPU*, not 80k.
- **`streams_per_gpu`** specifically requires the Day-4 `profile=gpu`
  deployment on rented hardware: run `live` mode with `--streams-live`
  stepping until a quality metric (measured_fps vs declared, batch latency)
  degrades, and record the knee. Until that run exists, the Terraform
  district module's `streams_per_gpu = 50` default and every node-count
  derivation from it is an estimate — the variable's own description says so.

### Extrapolation discipline

The harness measures at the step counts actually run (e.g. 5/50/500). The
80,000-camera claim is an extrapolation argument: measured per-message cost
× assumed rate, checked for linearity across the measured steps (a knee in
the p95 between steps invalidates the extrapolation and must be reported,
not averaged away). Any cell promoted from estimate to measured must state
the N it was measured at.

## 5. Known gaps the numbers must survive

- **Postgres at estate scale.** `camera_heartbeat` at 8,000 writes/s and the
  `camera_current` read-time staleness view are the first places 80k breaks;
  local Postgres measurements say so explicitly, and TimescaleDB hypertable
  behaviour is a cloud-run measurement, not an assumption.
- **Single-driver bottleneck.** The Python emitters in the harness saturate
  before the platform does at high step counts; `summary.md` reports achieved
  rates so a harness-limited run reads as such.
- **The bus at 80k.** Redis Streams carry alerts/detections today; Redpanda
  is the stated scale-test target. The detection-stream XADD rate is measured
  by the harness; the broker swap is not yet exercised.
