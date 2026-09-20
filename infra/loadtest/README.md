# PRAHARI load-test harness

Generates synthetic camera load against the **real** platform and records the
numbers `docs/SCALE-80K.md` cites. The rule from CLAUDE.md applies here first:
every scalability figure in that doc must trace to a recorded run under
`runs/` — this directory is how those runs get produced.

## What it is

A stepped driver (`run`) that, for each camera-count step (e.g. `5,50,500`):

1. **Seeds** N synthetic cameras through the registry's real REST API
   (`POST /api/v1/cameras`, the Model-2 manual-registration path).
2. **Emits worker heartbeats** per camera at the configured interval
   (`POST /api/v1/cameras/{id}/heartbeat`, same payload shape the inference
   worker sends — `worker.py`).
3. **Streams synthetic `VehicleDetection`s** over the real gRPC link —
   `MetadataIngestService.StreamDetections` on the match engine, the same RPC
   workers use (`proto/prahari/v1/adapter.proto`). A configurable fraction
   carry plates drawn from `data/watchlist/` so a hit exercises the real
   Bloom → fuzzy-match → dedup → alert path.
4. **Samples the read path** while the write path is under load: camera list,
   summary, geojson, and a correlation route build (`GET /api/v1/routes/{plate}`).
5. **Measures detection→alert latency** by matching injected `detection_id`s
   against the match engine's `/api/v1/alerts` ring buffer.
6. **Captures per-service CPU/mem** via `docker stats` (or `kubectl top -n
   prahari`) when available.
7. **Decommissions** the seeded cameras at the end (soft delete; use
   `--no-cleanup` to keep them for poking).

Each run writes `runs/<timestamp>-<label>/` with `config.json`,
`samples.jsonl` (every raw measurement, one JSON per line) and `summary.md`
(percentile tables). `runs/` is gitignored — promote a summary by copying it
somewhere committable and citing it from `docs/SCALE-80K.md`.

## Two modes — and what each honestly measures

| | `simulate` (default) | `live` |
|---|---|---|
| Cameras registered | real API calls | real API calls |
| Pixels | **none** — no decode, no inference | real ffmpeg→MediaMTX streams |
| Heartbeats | synthetic, emitted by the harness | real inference workers |
| Detections | synthetic, injected over gRPC | real pipeline — the gRPC injector still runs too unless `--detections-per-camera-s 0` |
| Measures | registry write/read throughput, health verdicts, match-engine ingest rate, alert latency, route-build latency | the full pipeline end-to-end on N real streams |
| Does NOT measure | video decode, inference, MediaMTX fan-out | per-GPU capacity without `profile=gpu` |

`simulate` answers "does the metadata plane hold at N cameras". It cannot
answer "does a GPU decode N streams" — that is what `live` mode plus the
Day-4 `profile=gpu` deployment is for.

## Quick start

```bash
# Sanity: full plumbing check against in-process fakes. No services needed.
cd infra/loadtest
./run.sh selftest

# Real run against the local k3d platform (make up first), services
# port-forwarded or reachable on localhost:
kubectl port-forward -n prahari svc/prahari-registry 8000:8000 &
kubectl port-forward -n prahari svc/prahari-match-engine 8001:8001 9001:9001 &
kubectl port-forward -n prahari svc/prahari-correlation 8002:8002 &

./run.sh run --cameras 5,50,500 --duration-s 60
# or: make loadtest LTARGS="run --cameras 5,50,500 --duration-s 60"
```

When the platform arms `internal_token` (any real profile), export
`PRAHARI_INTERNAL_TOKEN` — it is sent as `X-Internal-Token` on REST and
`x-internal-token` on gRPC, matching both gates.

### Live mode

```bash
./run.sh run --mode live --cameras 20 --streams-live 5 --duration-s 120
```

`--streams-live` ffmpeg publishers push `testsrc2` video to
`rtsp://localhost:8554/lt-src/<i>` on a MediaMTX container the harness starts
(`prahari-loadtest-mediamtx`). Cameras register with `rtsp_url` from
`--camera-rtsp-template` (default `rtsp://localhost:8554/lt-src/{i:05d}`) —
set it to an address reachable from wherever the registry-configured MediaMTX
runs (in k3d: `rtsp://prahari-mediamtx:8554/lt-src/{i:05d}`). After seeding,
the driver calls `POST /api/v1/streams/reconcile` so the `cam-<id>` pull
paths exist, then real inference workers pull the `fanout_rtsp_url`s.

Caveats, plainly:

- If no inference worker is running, live-mode cameras stay `unreachable` —
  that is the honest reading, not a harness bug.
- `testsrc2` contains no vehicles; the detection→alert path in live mode is
  exercised only if the video actually produces detections. Use
  `--detections-per-camera-s > 0` (the injector still runs in live mode) to
  exercise the metadata plane alongside the streams.
- ffmpeg `drawtext` needs a fontconfig-resolvable font; if plate text does
  not render, OCR sees nothing and the alert path stays quiet.

## Self-test

`./run.sh selftest` brings up `FakeRegistry` (stdlib HTTP, in-process,
implements the endpoints the driver touches) and `FakeIngestServicer` (a
counting `MetadataIngestService`), runs a 5-camera / ~5-second run against
them, and asserts: cameras seeded, heartbeats accepted, gRPC detections
acked, `samples.jsonl`/`summary.md` written, cameras decommissioned. The fake
registry does **not** emulate PostGIS scoping, health-verdict derivation, or
auth — a green selftest proves the harness's wiring, not platform
performance.

`./run.sh fake-registry --port 18000` serves the stub standalone for manual
curl poking.

## Dependencies

`httpx` + `prahari-proto` (the workspace's generated gRPC stubs — run
`make proto` first on a fresh clone). The package is a uv workspace member
(`infra/loadtest` in root `pyproject.toml`), so `uv run` / `./run.sh` resolve
it. If stubs are absent the detection leg is skipped and the run records
`grpc.unavailable` rather than failing silently.

## Limitations — read before citing numbers

- **Extrapolation is the user's job.** A step list of `5,50,500` measures at
  those counts. Scaling to 80,000 is an argument made in `docs/SCALE-80K.md`
  from measured per-camera/per-message costs and observed linearity — never
  an implicit claim of this harness.
- **One driver process.** At very high camera counts the Python emitters
  themselves become the bottleneck; the summary reports achieved rates so a
  saturating harness is visible in the data rather than hidden.
- **`e2e.alert_latency_ms` is floored at `--alert-poll-s`** (default 0.5 s).
  It measures "alert visible", not "matcher scored".
- **`live` mode capacity is host-bound.** ffmpeg publishers and MediaMTX on
  one laptop CPU do not resemble a district edge node.
- **Heartbeats in `simulate` are idealised** — always connected, ~declared
  fps. They load the write path; they do not exercise degraded/unreachable
  verdict transitions.
- **No Postgres-isolation bugs found here.** The fake registry in selftest
  and a tiny local Postgres differ wildly from production PostGIS+Timescale
  behaviour at 80k rows — measured registry numbers cite the database they
  ran against in `config.json`.
