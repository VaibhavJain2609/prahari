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
            │ same protobuf on the bus (Redis Streams today;
            │ Redpanda planned for the scale profile)
   ┌────────┼────────┬──────────────┐
   ▼        ▼        ▼              ▼
registry  correlation  BFF ──── Next.js console
+ PostGIS  route recon  REST+SSE   MapLibre (WHEP preview planned)
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
packages/       prahari-common — catalogue client, shared by registry + workers
services/       registry · inference · match-engine · correlation · bff
web/            Next.js 16 console (see web/README.md)
infra/
  helm/         umbrella chart; profile: local | gpu is the ONLY cutover knob
  k3d/          local cluster — same Kubernetes API as the cloud
  terraform/    modules/district — statewide rollout as a runnable artifact
docs/           PLAN · HLD · SECURITY · DAY2-DESIGN · DAY3-DESIGN ·
                ORG-TIERS-DESIGN · NEXT-PHASE-PLAN (on branch
                docs/next-phase-plan — post-submission hardening plan)
                Planned, not yet written: SCALE-80K · COST-MODEL · DEMO-SCRIPT
```

## Local first

Everything is built and debugged on `k3d` on a laptop before a single GPU hour
is spent.

```bash
make cluster          # k3d cluster + local registry
make proto            # generate protobuf stubs
make images           # build service images into the k3d registry
make up               # helm upgrade --install, profile: local
make gateway-secret   # load .env into the cluster (never into values.yaml)
make verify           # render both profiles and diff them
make test             # pytest across the workspace
```

`make dev` runs Tilt against the same chart, with live-reload on the registry.
Tilt applies no YAML of its own — Helm and Terraform in `infra/` are the only
source of truth, in the inner loop as much as on demo day.

`make up PROFILE=gpu` is the entire cloud cutover. The `profile` value swaps
model size, decode backend (VideoToolbox → NVDEC), sampling rate, stream cap,
batch size, GPU resource requests and KEDA autoscaling — all as *values*, never
as code paths.

> **The invariant:** if a cutover ever needs a code change, the switch is wrong.
> Fix the switch, not the code.

k3d on macOS has no GPU passthrough, so local runs are CPU/MPS with `yolov8n` at
2 fps over ~5 streams. That proves pipeline **correctness**; it cannot produce
the scaling curve. The curve comes from Day 4 on real hardware.

### Quick local run, no cluster

For iterating on the registry, BFF, or console without paying for a k3d
cluster, run the three services that back the web console directly. This is
what actually gets a browser to `http://localhost:3000` today — the steps
below are verified against a real login, not aspirational.

```bash
make proto   # protobuf stubs — imports fail without this

# Postgres: the registry and BFF share one database, credentials below.
docker run -d --name prahari-postgres -p 5432:5432 \
  -e POSTGRES_USER=prahari -e POSTGRES_PASSWORD=prahari -e POSTGRES_DB=prahari \
  postgis/postgis:16-3.4

# registry — applies its own migrations at startup, including the seed
# org "gj" (Gujarat) that the bootstrap admin below needs to already exist
cd services/registry && uv run uvicorn prahari_registry.app:app --port 8000 &

# bff — plain-HTTP session cookie and a bootstrap admin for the very
# first login (see BFFSettings.bootstrap_admin_*: a no-op once any user
# exists, so it's safe to leave set)
cd services/bff && \
  PRAHARI_SESSION_COOKIE_SECURE=false \
  PRAHARI_REGISTRY_BASE_URL=http://127.0.0.1:8000 \
  PRAHARI_BOOTSTRAP_ADMIN_USERNAME=admin \
  PRAHARI_BOOTSTRAP_ADMIN_PASSWORD=<pick one> \
  uv run uvicorn prahari_bff.app:app --port 8001 &

# console — proxies to the BFF at PRAHARI_BFF_URL, defaulting to :8001
cd web && npm run dev
```

Sign in at `http://localhost:3000/login` with the bootstrap username/password.
`PRAHARI_REDIS_URL` is left unset above, so the SSE alert stream reports 503
("alert stream not configured for this deployment") — expected, not a
failure. Catalogue sync will also log a warning if `.env`'s gateway
credentials aren't set; the registry runs fine without it. This path skips
Helm/Terraform entirely and is for local dev only — it is not the deployment
story (see "Deployment" in `CLAUDE.md`).

## Statewide rollout

```bash
terraform apply -var district=rajkot -var camera_count=4200
```

The `district` module derives GPU node count as
`ceil(camera_count / streams_per_gpu)` from the *measured* streams-per-GPU
figure, so the deployment arithmetic and the capacity claim in `docs/SCALE-80K.md`
cannot drift apart. Nothing district-specific is hardcoded in the module body.

## Status

Through Day 3's gate, local-first on k3d: the full vertical slice runs on a
laptop. `services/registry` (FastAPI + PostGIS, org-scoped RBAC, catalogue
sync, live camera health) and `services/inference` (decode → motion gate →
YOLO → OCR, lazily-loaded backends) landed Day 1/2, alongside the confusion-aware
`match-engine` and its watchlist gate test. Day 3 added `services/correlation`
(cross-camera stitching with spatio-temporal feasibility gating), `services/bff`
(auth, sessions, API keys, org-scoped proxying, SSRF-hardened RTSP probing, bulk
CSV import, hash-chained audit log, SSE), and the `web/` Next.js console
(auth-gated board, MapLibre camera health map, plate trace, alert console,
onboarding and admin panels) — gated by an executable test: plate in, a
timestamped route out, with an injected impossible hop rejected and a
non-watchlist plate correctly producing no alert.

Not yet done: the Day 4 cloud cutover (`profile=gpu` Helm switch is written and
`terraform apply` for a district is `validate`-clean, but neither has been run
against rented GPU hardware), the measured streams-per-GPU figure that
`docs/SCALE-80K.md` and the Terraform module's node-count math both depend on,
and the Day 5/6 load test, `docs/COST-MODEL.md`, and demo submission
artifacts. (`docs/HLD.md` and `docs/SECURITY.md` have since been written.)
See `TODO.md` for the day-by-day checklist and `CLAUDE.md` for the hard
invariants — several of them (RTSP over TCP, never trust `CAP_PROP_FPS`, feeds
loop) come straight from the portal's Integrator's Guide and will otherwise
cost real debugging hours. The submission window has now passed; what comes
next — including the security and deployment gaps this README does not hide —
is `docs/NEXT-PHASE-PLAN.md` (on branch `docs/next-phase-plan`).
