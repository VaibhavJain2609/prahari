# first-fix.md

Handoff from the Day 2 cross-verification pass (loop 2), written 2 Sep 2026.
The session that produced this was stopped on a CRITICAL cost flag before the
fixes were applied.

**Status update, 2 Sep 2026 (later the same day): all 20 findings below (M1-M6,
V1-V4, P1-P7, I1-I4) are fixed in the working tree.** Verified by re-reading
every file/line cited below against current source, not by trusting comments:
M1's bloom funnel is built from `Watchlist.bloom_keys()` and probed with
`bloom_probes()` in `matcher.py`; V1's pairing carries `vehicle_index` from
`process_batch` instead of reconstructing it positionally; M4/M5 project
`char_confidence` through `project_confidences` and re-derive `observed` from
`raw_text`; P5's `WatchlistStore` swaps one `WatchlistSnapshot`; M6 checks
`HasField("wall_clock")`; M2/M3 renamed the settings fields and added
`test_match_settings.py`'s bidirectional chart-parity check; P1-P4, P6, P7,
I1-I4 and V4 each have an inline comment citing their finding ID at the fix
site. `uv run pytest -q` -> 290 passed, `uv run ruff check .` clean, both
confirmed 2 Sep 2026 in the same session that added the Day 2 gate test
(`tests/test_day2_gate.py`, which exercises the real, now-fixed matcher).

Kept below verbatim as the record of what the review found and why each fix
looks the way it does -- the fix-site comments (`M1`, `V1`, `P5`, ...) refer
back to the sections here. Do not re-open any of these without re-reading the
current code first: several fixes changed the file's structure enough that
the original line numbers below no longer point at the described code.

Findings came from four independent reviewers, each reviewing code it did not
write, per `CLAUDE.md` > "How features get built" step 3. Three seams were
caught independently by two reviewers each — noted inline as `[2x]`. That
convergence is the reason to trust those three in particular.

---

## 1. State of the tree

Green, and misleadingly so — every defect below is in code that passes:

```
uv run pytest -q          250 passed
uv run ruff check .       clean
uv run ruff format --check .   clean (78 files)
make verify               both profiles render
helm lint / buf lint      clean
```

Fixed during integration (loop 3), already committed to the working tree:

- `services/match-engine/tests/test_grpc_server.py` — referenced
  `events_pb2.StreamTime`; `StreamTime` is defined in `common.proto`. Now
  `common_pb2.StreamTime`. This was the collection error left over from the
  interrupted session.
- 19 ruff findings in the match-engine tree: import ordering (I001), an unused
  `NullPublisher` import in `app.py`, and 10 over-long lines. Two trailing
  comments in `bloom.py` / `dedup.py` that `ruff format` had wrapped around
  their own statement were moved above the statement instead.

### The one incomplete edit

`services/match-engine/src/prahari_match/watchlist.py` — added
`Watchlist.bloom_keys()`, which yields `self._by_skeleton` **and**
`self._by_deletion`. It is purely additive and **nothing calls it yet**.
Finding M1 below is the other half. Either finish M1 or delete the method; an
unused method that looks like a fix is worse than no fix.

---

## 2. Findings

Severity order. `[2x]` = independently found by two reviewers.

### M1. Bloom prefilter has a guaranteed false negative — CRITICAL

`services/match-engine/src/prahari_match/matcher.py:192`, built at
`services/match-engine/src/prahari_match/app.py:51-52`.

The filter is built from `watchlist.skeletons()` only — full-length skeletons.
Stage 1 returns `no_match` unless the *observed* skeleton is itself in that set.
But stage 2 (`matcher.py:87-94`) exists specifically to handle observations
whose **length** differs from the watchlist entry. The two are mutually
exclusive, so `by_deletion_variant` / `single_char_deletions` are dead code.

Failure: watchlist holds `GJ01AB1234`; OCR reads `GJ01AB134` (dropped the `2` —
a routine plate-crop failure). Skeleton `6J01A8134` was never added → `no_match`.
The vehicle was seen, the watchlist had it, nothing surfaced. It matches today
only via the ~1% Bloom false-positive rate.

Self-inconsistent on top: `weighted_levenshtein` charges `_INDEL_COST = 1.2`
against a floor of `match_weak_score = 0.25` (`exp(-1.2/3) ~= 0.67`), so the
matcher is *willing* to accept a distance-1 indel that stage 1 will never show
it.

Fix: make stage 1's acceptance set a **superset** of stage 2's candidate set.
Three cases must all survive:

| observed vs entry | stage 2 path | what stage 1 needs |
|---|---|---|
| same length, confusion-class substitutions | `exact_skeleton` | observed skeleton in filter (already true — `skeleton()` folds the classes) |
| observed 1 shorter (dropped char) | `by_deletion_variant(observed)` | deletion variants of each watchlist skeleton in the filter |
| observed 1 longer (hallucinated char) | `single_char_deletions(observed)` | probe the filter with observed's deletion variants too |

So: build from `Watchlist.bloom_keys()` (already written), and probe with
`observed_skeleton` followed by `single_char_deletions(observed_skeleton)`.

Sizing consequence: the key set grows ~11x (1 + plate length), and the multi-probe
lookup amplifies the effective false-positive rate to ~11p. Size
`_build_bloom` off `len(list(watchlist.bloom_keys()))` rather than
`len(watchlist)`, and drop `bloom_target_fp_rate` to `0.001` so the funnel's
effective rate stays near 1%. At 20k entries that is ~395 KB — fine.

`tests/test_bloom.py:24` proves the *data structure* has no false negatives.
Nothing tests that the *funnel* has none, which is why this passes green. Add
that test.

Violates: `CLAUDE.md` "Never exact-match a plate against the watchlist";
`docs/DAY2-DESIGN.md` §7.2 stage 1 ("never a false negative").

### V1. Plates attributed to the wrong vehicle — CRITICAL

`services/inference/src/prahari_inference/detect/pipeline.py:189-195`.

`_pair_plates_to_vehicles` pass 2 walks unboxed plates positionally against
unclaimed vehicles. Its docstring claims "the surviving plates are a subsequence
of the vehicle list in the same relative order... walking both in order
reconstructs the original pairing exactly." False — a subsequence does not
record which elements it came from.

Verified by execution: vehicles `[car, bus, truck]`, reader returns `None` for
car and bus and a plate for truck → the plate is paired to **car**. The emitted
`VehicleDetection` carries `vehicle_class="car"` and the car's `vehicle_box`
with the truck's plate. Nothing errors.

The correct association exists at `pipeline.py:99-102`, where `read()` is called
per vehicle, and is then thrown away and incorrectly reconstructed. Fix: carry
the vehicle index alongside each plate from the call site instead of
re-deriving it.

Also: a boxed plate that finds no containing vehicle (`pipeline.py:185`) is
silently dropped — evidence discarded with no counter.

No test covers >1 vehicle with a gap (`test_pipeline.py:120-142` uses one
vehicle; `:197` uses the boxed path). Add one.

Affects Day 3 route reconstruction, which reads `vehicle_box`.

### M2. Four of six `PRAHARI_MATCH_*` chart envs do not exist as settings fields

`infra/helm/prahari/templates/_helpers.tpl:129-146` vs
`services/match-engine/src/prahari_match/config.py:14-19`.

| chart env | `MatchSettings` field | status |
|---|---|---|
| `PRAHARI_MATCH_GRPC_PORT` | `grpc_port` | ok |
| `PRAHARI_MATCH_DEDUP_BUCKET_S` | `dedup_bucket_s` | ok |
| `PRAHARI_MATCH_HTTP_PORT` | — | **ignored** |
| `PRAHARI_MATCH_WATCHLIST_PATH` | `watchlist_dir` | **ignored** |
| `PRAHARI_MATCH_MIN_SCORE` | `match_weak_score` | **ignored** |
| `PRAHARI_MATCH_CONFIRM_SCORE` | `match_confirmed_score` | **ignored** |

`SettingsConfigDict(extra="ignore")` (`config.py:18`) drops them without a word.

Failure (a): `values.yaml:154-155` sets `minScore: 0.72 / confirmScore: 0.90`;
the pod runs the code defaults `0.25 / 0.85`. An operator tuning precision on the
day changes nothing, silently, and "we tuned thresholds against measured
precision/recall" becomes false on the deployed system.
Failure (b): `watchlistPath: /var/lib/prahari/watchlist` is mounted and never
read — the service reads the relative default `data/watchlist` against the
container CWD, loads 0 entries, and `/readyz` 503s forever. Loud, at least.

**The names cannot be reconciled without a rename.** `env_prefix` is
`PRAHARI_MATCH_`, so a field named `match_weak_score` would need
`PRAHARI_MATCH_MATCH_WEAK_SCORE`. Recommended direction: drop the redundant
`match_` prefix from the settings fields —

```
match_weak_score      -> weak_score       PRAHARI_MATCH_WEAK_SCORE
match_probable_score  -> probable_score   PRAHARI_MATCH_PROBABLE_SCORE
match_confirmed_score -> confirmed_score  PRAHARI_MATCH_CONFIRMED_SCORE
match_score_decay     -> score_decay      PRAHARI_MATCH_SCORE_DECAY
match_max_candidates  -> max_candidates   PRAHARI_MATCH_MAX_CANDIDATES
```

then set the chart to those names plus `WATCHLIST_DIR`, `HTTP_PORT`. Mechanical
sed across `matcher.py`, `app.py`, `config.py`, tests. Note the code has a
three-band scheme (weak/probable/confirmed); the chart's two-value min/confirm
vocabulary is the poorer model — keep the code's.

### M3. No chart-parity test exists for the match engine

`grep -rn "infra/helm\|templates/" --include "*.py" services packages` returns
only `services/inference/tests/test_detector_settings.py` (inference-scoped) and
`services/inference/src/prahari_inference/config.py:102`.
`_helpers.tpl:122-123` **claims such a test exists**. It does not. That is why
M2 was structurally invisible.

Reverse direction too: `probable_score`, `score_decay`, `max_candidates`,
`bloom_expected_entries`, `bloom_target_fp_rate`, `dedup_max_entries`,
`redis_url`, `redis_stream_key`, `recent_alerts_size` are tunable fields the
chart never sets. `redis_url` in particular means the "one schema, two
transports" alert bus is unconfigurable from Helm, so `app.py:81` logs "alerts
fan out only to /api/v1/alerts" in **every** deployed profile.

Violates `CLAUDE.md` "Every `PRAHARI_*` env the chart sets must exist as a
settings field, asserted by a test that parses the template." Write that test,
both directions.

### M4/V2. `char_confidence` reaches the matcher index-misaligned `[2x]`

`services/match-engine/src/prahari_match/matcher.py:205` -> `confusion.py:216`,
against `services/inference/src/prahari_inference/detect/pipeline.py:215-233`.

`match()` does `confidences = tuple(plate.char_confidence)` and hands it to
`weighted_levenshtein(observed=plate.normalised_text, ...)`, which indexes it by
position in the **normalised** string. `events.proto:41` and `pipeline.py:216-227`
deliberately align `char_confidence` to **`raw_text`**.
`confusion.py:192` states "`confidences` is index-aligned with `observed`".
Those are different strings.

Failure: OCR reads `IND GJ 01 AB 1234` (HSRP, documented as real input at
`plates.py:61-66`). Normalised is `GJ01AB1234`; confidences has 17 entries
against the raw string. Normalised position 6 is `B`; `confidences[6]` is a
space's confidence. Every substitution is priced by the wrong glyph — a
same-class cost swings between 0.1 and 0.4 on that factor alone, enough to move a
hit across the WEAK/PROBABLE boundary in either direction. `_confidence_at` is
bounds-safe, so nothing crashes and nothing raises.

`prahari_common.plates.project_confidences` exists for exactly this
re-alignment and is **called from nowhere in either service**.

Fix: `project_confidences(list(plate.char_confidence), normalise_plate(plate.raw_text))`,
falling back to the raw tuple when `raw_text` is empty. One side must own the
projection — decide which and write it into the seam's docstring.

Currently masked because `PaddlePlateReader` broadcasts one uniform value across
all characters (`plates.py:68`), so every misaligned lookup returns the same
number. **The bug appears the day a real per-character OCR backend lands** —
i.e. on the GPU box, on Day 4. `test_matcher.py:_reading()` only ever builds
confidences already the length of the normalised text, so the suite cannot see it.

Violates `CLAUDE.md` "`char_confidence` must reach the match engine intact".
Not collapsed to a scalar — desynchronised, same symptom.

### M5. The observed plate is trusted from the wire and never re-normalised

`services/match-engine/src/prahari_match/matcher.py:185`.

Watchlist entries are normalised through `prahari_common.plates` on load
(`watchlist.py:111`); the observed side uses `plate.normalised_text` verbatim.
`proto/` is an explicitly vendor-neutral contract with a `CameraAdapter` plugin
ABI, so a non-Python adapter can set `normalised_text = "GJ-01-AB-1234"`, or
leave it empty with `raw_text` populated. Hyphens fold into the skeleton, bloom
misses, `no_match` — no error, no log.

Violates the "inference and the matcher must agree exactly on what a plate looks
like" invariant, in the one direction nothing enforces. Since M4 already requires
`raw_text` at this line, re-deriving `normalise_plate(plate.raw_text)` and
logging on disagreement with the wire value closes both at once.

### M6. A detection with no `wall_clock` collapses dedup to one alert forever

`services/match-engine/src/prahari_match/grpc_server.py:101-102`.

`detection.observed_at.wall_clock.ToDatetime()` on an unset field yields the
epoch → `wall_clock_s == 0.0` → `floor(0/8) == 0` for every detection,
unvalidated. First sighting alerts; every later sighting of that plate on that
camera, hours or days later, is suppressed as a duplicate.

Fix: `detection.observed_at.HasField("wall_clock")`; on absence, log once and
fall back to server receipt time so dedup still buckets sanely. Do not silently
accept the epoch.

Violates `docs/DAY2-DESIGN.md` §7.3 ("the same plate passing again an hour later
is a new alert"). Canonical "returns an answer, looks healthy, quietly wrong" —
the alert count just drops.

### I1. A dead pump is never restarted and never reaped

`services/inference/src/prahari_inference/worker.py:393-397`.

`_reap_finished_pumps` reaps only `camera_id not in self._assignments`.
`worker.py:361` (`added_ids`) starts a pump only for ids *not* in
`_assignments`. A camera that is still assigned but whose pump thread has ended
matches neither: never restarted, never reaped, and it permanently occupies one
of `max_active_cameras`.

Scenario: `tamper.observe()` raises once on a malformed frame (`cv2.calcHist` on
a zero-height array from a decoder hiccup). `worker.py:307` catches it, the
thread ends, `finally` closes the capture. That camera never decodes another
frame for the life of the pod, and with `max_active_cameras=5` a sixth camera can
never be added, because `available` (`worker.py:362`) is computed from
`len(self._assignments)`, which still counts the dead one.

Quieter variant: `capture.py:177-181` returns from `frames()` *normally* when
`camera.live` is False. The pump exits with `last_error=None`, so the heartbeat
reports `connected:false, last_error:null, frames_decoded:0` forever —
indistinguishable from a camera still starting up.

Fix: reap on `not thread.is_alive()` as well as unassignment, and restart a
still-assigned camera whose pump died (with backoff, so a permanently bad camera
does not spin).

### I2. The liveness probe cannot detect the failure its own comment describes

`services/inference/src/prahari_inference/worker.py:431-451`;
chart probe at `infra/helm/prahari/templates/inference.yaml:76-88`.

`_touch_liveness()` is called from `_report_loop` unconditionally and consults
nothing about pump threads; the probe only stats the file's mtime.
`worker.py:436-437` says the probe catches "the process alive, the pod Ready, and
every pump thread dead" — that is precisely the state it **passes**. The reporter
thread keeps turning, the file keeps getting touched, the pod stays Ready.

With I1, a worker with every pump dead runs Ready and un-restarted indefinitely,
heartbeating `connected:false` for every camera. Both the code comment and
`inference.yaml:73-74` assert the opposite.

Fix: touch the file only if every assigned camera has a live pump thread.

### I3. `connected` latches True and is never cleared during a reconnect

`services/inference/src/prahari_inference/worker.py:284-292` is the only writer of
`connected=True`; the only clears are in the pump's `except`/`finally`
(`worker.py:312, 317`). `StreamCapture.frames()` reconnects *inside* the
generator without returning, so the `for` body simply stops executing.

Scenario: camera drops at T. The capture enters backoff (up to 30 s per attempt,
indefinitely). Every 10 s the worker POSTs `connected: true`, the unchanged
`frames_decoded`, and the last `measured_fps`. Five minutes later the registry is
still told the worker is connected. `last_frame_at` staleness in `camera_current`
can rescue the verdict, but `connected` is an observation the worker is
contracted to report accurately, and it is factually wrong.

Violates `CLAUDE.md` "Workers observe" — the observation itself is stale.

### I4. `consecutive_failures` and `last_error` are structurally dead

`services/inference/src/prahari_inference/worker.py:290` sets
`consecutive_failures=0` on every frame; nothing anywhere increments it. The real
counter is a local inside `capture.py:196` and never leaves the capture.
`capture.py:207` logs "read failed %d consecutive times; reconnecting" but never
surfaces it — `stats.last_error` stays `None`.

Scenario: a camera has failed to connect for an hour. Its heartbeat says
`consecutive_failures: 0, last_error: null` — a plausible-looking zero that means
"no read failures" when there have been thousands. The module docstring
(`worker.py:11-13`) promises the worker reports decode counts and its last error.

Fix: surface the capture's counter and last exception onto `CameraStats`.

### P1/V3. One exception in `on_batch` permanently disables the deadline flush `[2x]`

`services/inference/src/prahari_inference/detect/batching.py:79-86`.

`_flush_loop` has no `try/except` around `self._flush_if_overdue()`. `_on_batch`
runs the whole cascade (`process_batch` -> YOLO -> OCR); any exception propagates
out, ends `_flush_loop`, and the daemon thread dies. `close()` still `join()`s
successfully and `submit()` still works. From then on **only the size trigger
fires**: on a quiet estate (active cameras < `batch_size`) frames accumulate in
`_pending` unboundedly and the 5 s alert budget is missed without limit — no log,
no counter, no liveness signal.

This is the single-failure-kills-the-timer case the module docstring says it
exists to prevent. Violates `CLAUDE.md` "flush on batch size *or* a deadline,
whichever comes first."

Fix: wrap the loop body, log the exception, keep the thread alive.

### P2. `close()` drops the pending batch

`services/inference/src/prahari_inference/detect/batching.py:114-116` sets
`_stop` and joins; it never calls `flush()`. Up to `batch_size-1` sampled frames
are discarded at every shutdown. It also ignores the `join(timeout=1.0)` result,
so a wedged flush thread is indistinguishable from a clean stop.

### P3. `MotionGate` state mutated from multiple threads with no lock

`services/inference/src/prahari_inference/detect/pipeline.py:57-62` +
`detect/motion.py:66-105`.

`CrossCameraBatcher` invokes `_on_batch` on *whichever pump thread filled the
batch* (`batching.py:77`) and also on the flush thread (`:99`), so
`process_batch` runs concurrently. Two concurrent batches both containing
`cam-1`: (a) `_gate_for` is check-then-act — both see `None`, both construct a
`MotionGate`, one is dropped, and the background model plus
`frames_seen`/`frames_passed` reset; (b) even with one gate, `self._background`
is read at `motion.py:93` and rewritten at `:98` unguarded, so one thread diffs
against a background another already replaced.

Result: bogus `changed_fraction`, and a corrupted `skip_ratio` — **the number the
whole streams-per-GPU argument rests on**, and therefore the Day 4 measurement
and every capacity figure downstream.

### P4. `Deduper` is not thread-safe and is called from a 10-thread gRPC pool

`services/match-engine/src/prahari_match/dedup.py:48-59`;
`grpc_server.py:102` calls `should_alert` from `MetadataIngestServicer`, served by
`ThreadPoolExecutor(max_workers=settings.grpc_max_workers)` (`grpc_server.py:120`).

Two workers streaming the same plate on the same camera in the same 8 s bucket
both evaluate `if key in self._seen` before either assigns → both return `True` →
two alerts for one dwell, which is precisely what the module exists to prevent.
Concurrent `move_to_end`/`popitem` on the same `OrderedDict` is also unsafe.

### P5. The watchlist swap is torn, and the docstring claims it is not `[2x]`

`services/match-engine/src/prahari_match/matcher.py:192,196` +
`app.py:161-174`.

`match()` reads `store.bloom` at `:192`, then `store.watchlist` at `:196`.
`WatchlistStore.replace` (`matcher.py:54-56`) is two separate assignments. A
`POST /api/v1/watchlist/reload` landing between those two reads gives new-bloom +
old-index: the detection passes stage 1, finds no candidate, and returns
`no_match` for a plate that **is** on the new watchlist.

`app.py`'s docstring asserts the opposite ("it only ever reads `store.watchlist` /
`store.bloom` as a whole, never field-by-field across the swap"), so the bug reads
as intended behaviour.

Fix: one immutable snapshot object swapped in a single assignment, and
`match()` takes `watchlist, bloom = store.snapshot()` once.

### P6. `watchlist_reload` is `async def` but does blocking disk I/O

`services/match-engine/src/prahari_match/app.py:161-174`. `_load_watchlist`
walks the directory and parses every JSON/CSV; `_build_bloom` hashes every
skeleton — all on the event loop. For a 20k-entry watchlist this stalls
`/healthz`, `/readyz` and every other HTTP request for the duration. Make it
`def` (FastAPI runs it in a threadpool) or use `run_in_threadpool`.

### P7. Remove-then-re-add orphans a live pump — two upstream pulls

`services/inference/src/prahari_inference/worker.py:361-368`,
`:393-401`. `_reap_finished_pumps` only reaps threads that are *not* alive. A
camera removed from the assignment set and re-added before its pump notices
leaves the original thread and capture running while a second pump starts.

Violates `CLAUDE.md` "One upstream pull per camera. Every client gets its own
copy of the stream" — on a shared government feed this is the invariant with an
external cost.

### V4. Replay-burst frames are not excluded from motion magnitude

`services/inference/src/prahari_inference/detect/motion.py:66-105`.
`docs/DAY2-DESIGN.md` §4.2: never gate on `replaying=True` frames for motion
*magnitude*. Replay frames are valid pixels and still go to detection, but
consecutive replay frames are milliseconds apart in wall time and seconds apart
in stream time, so the inter-frame difference is not comparable to a live frame's.

(This reviewer's report was truncated mid-finding; re-derive the detail from
`DAY2-DESIGN.md` §4.2 rather than paying to re-run the review.)

---

## 3. Suggested order

Fix in this order — earlier items are the ones that return a wrong answer while
looking healthy, and are what the Day 2 gate actually depends on.

1. M1 (bloom false negative) — finish the started edit, plus the funnel test
2. V1 (wrong-vehicle pairing) — plus a >1-vehicle-with-gap test
3. M4/V2 (`char_confidence` projection) — decide which side owns it
4. M5 (re-normalise observed) — falls out of M4
5. M6 (unset `wall_clock`)
6. M2 + M3 (settings rename, chart envs, parity test both directions)
7. P5 (snapshot swap), P4 (dedup lock), P3 (motion gate lock)
8. P1/V3, P2 (batching thread survival + flush on close)
9. I1, I2, I3, I4, P7 (worker robustness — matters for a long run, not the gate)
10. V4 (replay frames in motion magnitude)

Items 1-7 are what I would do before anything else. 8-10 can follow.

---

## 4. Still open from TODO.md, Day 2 (as of the original review)

Not defects — work never started at review time. **Both now done**, in the
same later session that closed out section 2's findings:

- ~~Wire the gRPC `MetadataIngestService` **client** in the inference worker~~
  -- done: `grpc_client.py` + `worker.py` wiring, tested in
  `test_grpc_client.py` and `test_worker_pipeline.py`.
- ~~The Day 2 gate as an executable test~~ -- done: `tests/test_day2_gate.py`,
  passing, under the 5 s budget, against the real (fixed) matcher.
- `TODO.md` now ticks both lines, since both are backed by a passing test, not
  just written code -- the condition the original note held out for.

---

## 5. Verification loop

Run together, to convergence, not once:

```
make proto
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
make verify
```

`make lint` also runs `helm lint` and `buf lint`.

Note for a fresh clone: `make proto` must run before the `prahari.v1` imports
resolve — the stubs in `packages/prahari-proto/src/prahari/` are gitignored.

---

## 6. Process notes

- Reviewers were read-only by construction and edited nothing; every finding
  above was re-checked against the source before being written here, except V4
  and the truncated tails.
- Four reports (`review-matcher`, `review-vision`, `review-ingest`,
  `review-python`) were truncated by message-size limits before their PLAUSIBLE
  sections. Everything above is from the CONFIRMED sections. There is a tail of
  lower-severity findings that was never transmitted; a re-review would surface
  them, but it is not worth the spend before these are fixed.
- The session was stopped on a CRITICAL cost flag ($115.74 and rising). Four
  parallel reviewers on Opus over ~3,200 lines is what cost that. If this is
  repeated for Day 3, run reviewers sequentially, or on a cheaper model for the
  language pass.
- **What the loop bought:** ~20 confirmed defects in a tree where 250 tests
  passed, ruff was clean, and both Helm profiles rendered. Three seams were found
  independently by two reviewers each. Nothing here would have been caught by
  running the suite.
