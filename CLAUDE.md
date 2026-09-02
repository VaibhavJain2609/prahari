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

**Camera health**
- **Workers observe; the registry decides.** An ingest worker reports
  connected/not, `measured_fps`, decode counts and its last error. It never
  computes a health state: two workers on the same camera would publish
  contradictory verdicts, and a worker cannot see that its own heartbeats have
  stopped arriving.
- **Staleness is computed at read time**, in the `camera_current` SQL view. No
  code runs when heartbeats stop, so nothing that runs on receipt can detect it.
- Drift is measured against the camera's **own** recent median rate, never
  against `declared_fps`. The declared rate is recorded and never used to judge.

**Detection & matching**
- **Inference never corrects a plate.** It emits `raw_text`, per-character
  confidences, `normalised_text` and a format. Correction belongs to the match
  engine, where it is scored and carries a `MatchExplanation`. A silently
  corrected plate is unexplainable in court.
- **`char_confidence` must reach the match engine intact.** The matcher prices
  each substitution by the confidence of the character it replaces. Collapsing
  it to one scalar does not fail — it just makes matching quietly worse.
- **Plate grammar lives in `prahari-common`, once.** Inference and the match
  engine must agree exactly on what a plate looks like; if they normalise
  differently every lookup misses and nothing raises. Tolerance (confusion
  classes, edit costs) belongs to the match engine, grammar to the shared
  package. Never duplicate either.
- **Never exact-match a plate against the watchlist.** OCR reliably confuses
  `0/O/D/Q`, `8/B`, `1/I/L`, `5/S`, `2/Z`, `6/G`. Exact matching fails the live
  test *silently*: the vehicle was seen, the lookup missed, nothing surfaced.
- **Every alert carries its justification** and is deduped on
  `(camera, plate, time-bucket)`. A vehicle in frame for 8 s at 3 fps is one
  alert, not 24.
- **Model backends are imported lazily**, inside `_load()`, never at module
  import — `import prahari_inference` must not pull in torch. Every stage is
  exercisable with no weights installed, or the test suite stops being run.
- **The device/decode backend is selected from settings, never sniffed.** No
  `torch.cuda.is_available()` branching: that is a code path the `profile`
  switch cannot see, on a machine no values file describes.
- **Batch across cameras, never within one.** One camera at 2 fps cannot fill a
  batch without adding half a second of latency. Flush on batch size *or* a
  deadline, whichever comes first.
- **Motion gate and tamper detector are scoped to `loop_epoch`** and reset
  across it. The background model is invalid after a scene cut, and a tamper
  detector that fires on every loop gets switched off on day one.

**Contracts**
- All inter-service messages are protobuf, defined in `proto/`. Regenerate stubs with
  `make proto`; never hand-edit generated code. Python stubs land in
  `packages/prahari-proto/` as an installable workspace package (generated imports are
  absolute, so the tree has to *be* a package) and are gitignored — the contract is the
  `.proto`, not the stub. A fresh clone must run `make proto` before the imports resolve.
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
- **Every `PRAHARI_*` env the chart sets must exist as a settings field**, asserted by a
  test that parses the template. A knob the chart writes and the code never reads is a
  profile switch that silently does not switch: the GPU profile looks applied, is not,
  and the number it was meant to change ends up on a slide.

**Privacy & evidence**
- Video never leaves the edge except as an explicit, audited evidence request.
- Every video access is written to the hash-chained audit log, with an actor and a purpose code.
  No exceptions, no "internal" bypass path.
- Face-derived data is handled under a stricter gate than plate data. Keep them separable.

---

## Layout

```
proto/prahari/v1/     the vendor-neutral contract — start here
packages/
  prahari-common/     catalogue client + gateway settings + plate grammar. Shared
                      because the registry and the ingest workers reach the same
                      gateway, and because inference and the match engine must
                      agree exactly on what a plate looks like. Deliberately has
                      no OpenCV dependency, so a service that only reads the
                      catalogue does not inherit a decoder.
  prahari-proto/      generated protobuf/gRPC stubs. `make proto` writes here;
                      gitignored except the package __init__ files.
services/
  registry/           FastAPI + PostGIS · catalogue sync · gap analysis · camera health
  inference/          decode · motion gating · YOLO + OCR · batching · gRPC client
    detect/types.py     the cascade's stage contracts — Protocols + dataclasses.
                        Every backend has a scripted fake beside it.
  match-engine/       confusion-aware fuzzy match · Bloom prefilter · dedup · alert fan-out
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
make proto            regenerate protobuf stubs
make images           build service images into the k3d registry
make up               k3d cluster + helm install, profile=local
make gateway-secret   load .env into the cluster as the credential Secret
make dev              tilt up (inner loop)
make test             pytest across the workspace
make lint             ruff + helm lint + buf lint
make verify           render both profiles and check the switch
make down             tear down the local cluster
```

Services are built from the **workspace root**, not their own directory —
each depends on `packages/prahari-common`, which a narrower Docker build
context would exclude:

```
docker build -f services/registry/Dockerfile .
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

### How features get built

Plan, then build in a team, then verify in loops. Not one pass.

1. **Design before code.** A feature gets a written design first — what it is, what
   breaks without it, and what it deliberately excludes. `docs/DAY2-DESIGN.md` is the
   worked example. The design exists so the reviews in step 3 have something to review
   *against*, rather than reviewing code against taste.

2. **Contracts are written centrally, before the team is dispatched.** Shared types,
   Protocols, settings classes and proto stubs are fixed first, by one hand. Then owning
   agents implement behind them in parallel with **disjoint file sets** — an explicit list
   of files each owns and an explicit list it must not touch. Two agents editing one file
   is a lost edit, not a merge conflict.

3. **Cross-verification, always by someone who did not write it.** Each feature is
   reviewed by an independent domain reviewer *and* an independent language reviewer.
   Reviewers check against the hard invariants above, not preference. Findings go back to
   the owner; the loop repeats until a full pass produces nothing new. A feature that
   passed because only its author looked at it has not been verified.

4. **Integration loop.** `make proto`, `make test`, `make lint`, `make verify` — run
   together, to convergence, not once. Then the day's gate as an executable test.

Loops are the deliverable as much as the code is. The failure mode this exists to prevent
is the one this whole system is designed around: something that returns an answer, looks
healthy, and is quietly wrong.

### Git workflow

- **Commit after every change.** Once a change is made and passes the loop above (or is
  otherwise a coherent, working unit — a design doc, a config fix, a test), commit it.
  Don't let work accumulate uncommitted across a session; an uncommitted change is not yet
  part of the project. Commit messages follow the existing log's style: what changed and
  why, not a restatement of the diff.
- **One feature, one branch, one worktree.** Each feature or fix gets its own branch, and
  that branch is checked out in its own `git worktree` — `git worktree add
  ../prahari-<feature> -b <feature-branch>` — rather than switching branches in place in
  this checkout. This is what lets more than one feature be in flight without one agent's
  half-finished edit landing in another's diff, and keeps `main` always in the state Day
  N's gate expects. Merge (or open a PR) back to `main` when the feature's loop converges;
  remove the worktree once it's merged.
- Origin is `github.com/VaibhavJain2609/prahari`, public. Nothing that fails the secrets
  scan (`.env`, real gateway hosts/passwords, captured `/api/ingest` snapshots — see
  `.gitignore`) ever gets committed, public repo or not.
