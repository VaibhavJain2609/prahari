# Day 2 design — detection → plate → match → alert

Referenced from `TODO.md` ("Day 2 — 3 Sep"). `docs/PLAN.md` is the *why* for the
whole project; this is the *how* for one day's worth of it. Written before the
code, so that the reviews in §8 have something to review against.

**Day 2 gate:** a known plate in a clip produces an alert in under 5 s, end to
end, with a match explanation attached.

---

## 1. What Day 1 left on the table, and where it lands here

| Day 1 gap | Closed by |
|---|---|
| `PRAHARI_INGEST_USE_HLS` documented, read by nothing | §2 — `IngestSettings.use_hls`, threaded to `StreamCapture` |
| Worker fetches assignments once, at startup | §2 — assignment reconciliation on the heartbeat tick |
| `PRAHARI_DETECT_*` set by the chart, read by nothing | §3 — `DetectorSettings`, names matched to the chart |
| `black_frame_ratio: null`, `tamper_suspected: false` | §6 — a real detector, loop-aware |

These are not side quests. Three of the four are knobs the Helm chart already
sets: a value the chart writes and the code never reads is a profile switch that
silently does not switch, which is worse than having no switch.

---

## 2. Ingest gaps (`services/inference`)

**HLS fallback.** `StreamCapture` already accepts `use_hls`; nothing sets it.
Add `IngestSettings.use_hls: bool = False` and pass it at the two construction
sites. The fallback only affects the *catalogue-derived* URL — a worker handed a
MediaMTX fan-out URL by the registry already has an explicit URL and ignores
transport preference entirely. That asymmetry is the point: the venue-day
failure ("8554 is blocked") is a gateway-side problem, and MediaMTX is the thing
that absorbs it.

**Assignment reconciliation.** The reporter thread already wakes every
`heartbeat_interval_s`. Re-fetch assignments on that tick and diff against the
running set:

- **added** → start a pump thread, subject to `max_active_cameras`
- **removed** → `request_stop()`, never `close()` — the pump thread owns its
  capture handle and releases it in its own `finally`. Releasing a
  `VideoCapture` from under a thread blocked in `read()` is an OpenCV segfault,
  not an exception.
- **unchanged** → left strictly alone. A camera must not have its stream
  interrupted because some *other* camera appeared in the catalogue.

The cap stays a hard cap across the diff. If the registry returns more cameras
than the cap, the excess is dropped at the worker, because the cost of getting
that wrong is paid by every other consumer of a shared government feed.

---

## 3. `DetectorSettings` — the contract with the chart

`env_prefix="PRAHARI_DETECT_"`, one field per name the chart already writes:
`MODEL`, `DECODE_BACKEND`, `BATCH_SIZE`, `MOTION_GATE`, `MATCH_ENGINE_GRPC`.

Two rules:

1. **No runtime sniffing.** The decode backend is *selected* by profile, never
   auto-detected. A pipeline that probes for CUDA behaves differently on the
   laptop and the GPU node for reasons no value file records, which is exactly
   the failure mode the `profile` invariant exists to prevent.
2. **A test asserts the field names match the chart.** Parse
   `templates/inference.yaml`, collect every `PRAHARI_DETECT_*` it sets, and
   assert each maps to a real field. This is the only mechanism that catches
   drift between a values file and a settings class, and the drift is silent.

---

## 4. The cascade (`prahari_inference.detect`)

```
Frame ──SampleGate──▶ MotionGate ──▶ VehicleDetector ──▶ PlateLocaliser ──▶ OCR
        (PTS, done)    (cheap)        (yolov8n)           (crop)            (text)
                            │                                                │
                            └── TamperDetector (runs on every sampled frame) │
                                                                              ▼
                                                              VehicleDetection (protobuf)
```

Cheap stages gate expensive ones. Concretely, on typical CCTV the motion gate
alone drops the large majority of sampled frames before a single tensor is
allocated, and that ratio *is* the streams-per-GPU number measured on Day 4.

### 4.1 Backends are protocols, with a deterministic fake

`VehicleDetector` and `PlateReader` are `typing.Protocol`s. Two implementations
each:

- a real one (Ultralytics YOLO; PaddleOCR), imported **lazily inside the
  constructor** so that importing `prahari_inference` never pulls in torch;
- a scripted fake that returns fixtures.

This is not test scaffolding for its own sake. `torch` + `ultralytics` +
`paddleocr` is a multi-GB install; if `make test` depends on them, the test
suite stops being run. Every stage boundary in this cascade must be exercisable
without a model file present.

**Import discipline:** `ultralytics`/`paddleocr` may only be imported inside
`_load()` methods. A ruff `banned-api` rule enforces it, in the same way
`TID251` already enforces the `rtsp_env` indirection for `cv2`.

### 4.2 Motion gate

Downscaled greyscale frame differencing against a running background, with a
dilation pass so a moving vehicle does not fragment into specks.

**Epoch-scoped.** `FrameTiming.loop_epoch` changing means the scene cut, and the
background model is invalid across it. Reset the model and pass the first frame
of the new epoch unconditionally. Failing to do this makes the gate fire on
every frame for several seconds after each loop cut — the exact opposite of what
it is for.

**Never gate on `replaying=True` frames for motion *magnitude*.** Replay frames
are valid pixels, so they still go to detection, but consecutive replay frames
are milliseconds apart in wall time and seconds apart in PTS; treating their
difference as "motion per second" is meaningless.

### 4.3 Batching across cameras, not within one

A single camera at `sample_fps=2` cannot fill a batch without adding 500 ms of
latency. Eight cameras at 2 fps fill a batch of 8 in ~60 ms. So the batcher
collects from a shared queue across all pump threads, and flushes on **either**
`batch_size` frames **or** a deadline — whichever comes first, so a quiet estate
never stalls a partial batch indefinitely.

The deadline is what keeps the 5 s gate honest under load.

---

## 5. Plate normalisation — in `prahari-common`, deliberately

`packages/prahari-common/src/prahari_common/plates.py`.

It goes in the shared package because **the inference service and the match
engine must agree, exactly, on plate grammar.** If inference normalises
`GJ 01 AB 1234` one way and the watchlist loader normalises it another, every
lookup misses and nothing errors. That is the silent-failure mode the whole
match-engine design exists to prevent, so the shared vocabulary cannot live in
either consumer. `prahari-common` stays dependency-light — this is pure regex,
no OpenCV.

Formats modelled (already in `events.proto` as `PlateFormat`):

| Format | Shape | Example |
|---|---|---|
| `STANDARD` | `SS DD LL NNNN` | `GJ 01 AB 1234` |
| `BH_SERIES` | `YY BH NNNN LL` | `21 BH 1234 AB` |
| `MILITARY` | `NN L NNNNNN L` | `12 A 123456 X` |
| `NONCONFORMING` | legible, does not parse | keep it |

Normalisation strips whitespace, hyphens, a leading `IND`, and uppercases.
`NONCONFORMING` is retained rather than dropped: a plate we cannot parse is
still evidence that a vehicle was there, and dropping it makes the route
reconstruction on Day 3 lie by omission.

**INVARIANT (already in `events.proto`): inference never corrects a plate.** It
emits `raw_text`, per-character confidences, `normalised_text`, and a format.
Correction happens in the match engine where it is scored and auditable. A
silently corrected plate is unexplainable in court.

### 5.1 Positional format templates

`STANDARD` gives a per-position character class: `LLDDLLDDDD`. This is worth far
more than generic fuzzy matching, because it turns an ambiguous glyph into a
near-certainty: a `0` read at position 0 must be a letter, so `O`/`D`/`Q` are
the only candidates. The template is produced here and consumed in §6.

---

## 6. Tamper and black-frame detection

Per sampled frame: mean luma → `black_frame_ratio` over a rolling window;
Laplacian variance → defocus/covered-lens; a histogram-correlation break against
the recent frame → scene change.

**The loop cut must not fire it.** These feeds are recordings that loop, and the
wrap is an abrupt scene change on every camera, every cycle. `PTSClock` already
detects it and increments `loop_epoch`; the tamper detector consumes that and
suppresses the scene-change signal across an epoch boundary, plus a short
settling window afterwards. It does *not* suppress the black-frame signal —
a feed that loops into darkness is genuinely dark.

The worker's heartbeat then reports real numbers instead of the current
`black_frame_ratio: null` placeholder. Null was deliberate on Day 1 (a console
must not show "no tampering detected" for a detector that is not running) and
is now retired.

---

## 7. `services/match-engine` — the highest-ROI component

Detection event → watchlist decision → alert.

### 7.1 Why exact matching fails, in one line

The plate was seen, OCR returned `GJ01AB1Z34`, the watchlist holds `GJ01AB1234`,
the lookup missed, and **nothing surfaced**. No error, no log, no signal. The
evaluators hand over a plate on the day; this is the failure we are engineering
against.

### 7.2 Three-stage funnel

Most traffic is not on the watchlist, so the design optimises for fast rejection.

**Stage 1 — skeleton + Bloom (O(1), rejects ~all traffic).**
Fold every confusable character onto a canonical representative:

```
O D Q → 0    B → 8    I L → 1    S → 5    Z → 2    G → 6
```

`GJ01AB1Z34` and `GJ01AB1234` both fold to `6J01A81234`. A Bloom filter over
watchlist skeletons rejects a non-match in constant time and constant memory —
which is what makes this viable at statewide event rates, where a per-event
scan of the watchlist is not.

**Stage 2 — candidate generation.** Skeleton-bucket exact hit, plus
deletion-neighbours to survive a dropped or spurious character. Bounded
candidate set, never a full scan.

**Stage 3 — weighted scoring.** Levenshtein where substitution cost is a
function of three things:

1. **Confusion class.** `0↔O` is cheap; `0↔W` is full price.
2. **Per-character OCR confidence.** A substitution at a character OCR was
   unsure of is cheap. This is why `PlateReading.char_confidence` must survive
   from inference intact — collapsing it to a single score is the most common
   way to accidentally cripple match accuracy, and `events.proto` calls it out
   as an invariant.
3. **Positional plausibility** (§5.1). A digit-for-letter swap in a letter
   position costs less than the same swap in a digit position.

Score, band (`WEAK`/`PROBABLE`/`CONFIRMED`), and a `MatchExplanation` carrying
every applied `CharacterEdit` with its cost. **Every alert records why it
matched.** Police act on these; an unexplainable match is worse than no match.

### 7.3 Dedup

Key: `(camera_id, matched_plate, floor(pts_wall / bucket_s))`. A vehicle in
frame for 8 s at 3 fps is one alert, not 24. Unbucketed alerting makes the
console useless within a minute, which is a demo-day failure, not a polish item.

### 7.4 Surfaces

- gRPC `MetadataIngestService` (client-streaming) — the worker link.
- FastAPI: `/healthz`, `/readyz`, watchlist admin, alert query. `/readyz`
  reports whether the watchlist is actually loaded; a match engine with an empty
  watchlist answers every query "no match" and looks perfectly healthy.
- Alerts fan out onto the bus as the **same protobuf messages**. One schema, two
  transports.

---

## 8. How this gets verified — loops, not a single pass

Every feature below is built by an owning agent and then attacked by agents that
did not write it. A finding goes back to the owner; the loop repeats until a
pass produces nothing new.

**Loop 1 — build (owner agent).** Code plus unit tests. Exit: `uv run pytest`
and `ruff check` green for that service.

**Loop 2 — cross-verification (two independent reviewers per feature).** One
domain reviewer and one language reviewer, neither of whom wrote the code.
Reviewers check against the hard invariants in `CLAUDE.md`, not against taste.
Exit: a full review pass yields no correctness finding.

**Loop 3 — integration.** The whole workspace: `make test`, `make lint`,
`make verify`, `make proto`. Plus the Day 2 gate as an executable test —
synthetic clip in, alert out, measured, with no model weights required.

The loops are the deliverable as much as the code is. A feature that passed
because only its author looked at it has not been verified.

---

## 9. Deliberately not in Day 2

- Correlation / route reconstruction (Day 3 — needs a populated detection store).
- The web console (Day 3). The Day 2 gate is asserted by a test, not by eyes.
- Real model weights in CI. Day 4 measures throughput on a GPU; Day 2 proves the
  wiring.
- Any streams-per-GPU number. Day 2 must not produce a figure that would end up
  on a slide unmeasured.
