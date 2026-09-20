# PRAHARI — High-Level Design

The one-page architecture. `docs/PLAN.md` is the *why* (including the build
schedule); `README.md` is the entry point; this is the *what connects to what
and why it is shaped that way*. Day-level designs live in `DAY2-DESIGN.md`,
`DAY3-DESIGN.md`, `ORG-TIERS-DESIGN.md`; enforcement truth in `SECURITY.md`.

## 1. The decision everything derives from

You cannot centralise 80,000 video streams: 80,000 × 2 Mbps = **160 Gbps** of
backhaul and **~52 PB** of 30-day storage. Centralising *metadata* instead
costs **< 250 Mbps** and **~6 TB/month** — roughly 650× and 8,600× less. So
the challenge's Reference Model 4 (Central VMS) is **explicitly rejected**,
with the arithmetic, rather than silently not implemented. Built instead:
**Model 1** (registry + GIS — mandatory for every entry) plus **Model 3**
(federation middleware), with Model 2 direct-connect as one adapter class.

When a design choice is ambiguous, keep pixels at the edge.

## 2. Three planes

- **Control plane** — `services/registry`: the camera registry + GIS.
  Small, authoritative, live. Owns camera state; workers only report
  observations to it.
- **Data plane** — video stays at the edge / the government gateway. Central
  access exists only as audited requests: live preview rides a scoped,
  expiring MediaMTX ticket minted *after* the `video_preview` audit row
  (`docs/SECURITY.md`), and the evidence-request chain (`docs/EVIDENCE.md`)
  records the clip pull — real clip bytes land when MediaMTX `record: yes`
  does; the ticket already grants `playback`.
- **Metadata plane** — detections, plates, tracks, alerts flow centrally as
  protobuf events (`proto/prahari/v1`), on gRPC for the high-rate link and
  Redis Streams for fan-out. One schema, two transports.

## 3. Components

```
 govt feeds (RTSP/HLS/WHEP)
            │  one upstream pull per camera — catalogue is the contract,
   ┌────────▼─────────── MediaMTX ───────────┐  never a URL template
   │        fan-out to N consumers           │
   └───┬──────────────────────┬─────────────┘
       │ RTSP (TCP)           │ WHEP (preview, planned)
┌──────▼───────┐              │
│ inference    │ decode → motion gate → YOLO → plate crop → OCR
│ workers      │ PTS-driven timing · loop-epoch scoping · tamper
│ (KEDA-scaled)│ cross-camera batching · lazy model backends
└──────┬───────┘
       │ gRPC client-stream: VehicleDetection (+ char_confidence intact)
┌──────▼───────┐  confusion-aware fuzzy match (0/O/D, 8/B, 1/I/L, 5/S, 2/Z, 6/G)
│ match-engine │  Bloom prefilter · (camera, plate, time-bucket) dedup
└──────┬───────┘  every alert carries a MatchExplanation
       │ XADD — two Redis Streams:
       │   prahari:alerts      (watchlist hits)
       │   prahari:detections  (every legible detection — a non-hit
       │                        must still be reconstructable later)
       │
┌──────▼────────┐   ┌──────────────┐   ┌─────────────────────────────┐
│ correlation   │   │ registry     │   │ BFF                         │
│ route recon   │◀──│ + PostGIS    │◀──│ auth · org-scoped proxy ·   │
│ feasibility-  │   │ orgs (ltree) │   │ purpose codes · hash-chained│
│ gated hops    │   │ camera health│   │ audit (SQLite) · SSE · CSV/ │
└──────┬────────┘   │ gap analysis │   │ PDF export                  │
       │            └──────▲───────┘   └──────────────┬──────────────┘
       │                   │ heartbeats               │ /api/bff/* proxy
       └───────────────────┴──────────────────────────▼──────────┐
                              │        web/ — Next.js 16 console │
                              └─────────────────────────────────┘
```

## 4. Data flow: detection → alert → console

1. A worker pulls its assigned cameras from the **MediaMTX fan-out** (never a
   second upstream pull), samples at `sample_fps`, motion-gates, batches
   *across* cameras, and runs YOLO → plate crop → OCR. It emits `raw_text`,
   per-character confidences and `normalised_text` — **inference never
   corrects a plate**; correction is scored downstream where it stays
   explainable.
2. Detections stream over gRPC to the **match-engine**: Bloom prefilter →
   weighted-edit-distance fuzzy match over the OCR confusion classes →
   dedup → `Alert` on `prahari:alerts`; *every* detection also lands on
   `prahari:detections`, because the mandatory test case (plate → route) does
   not require a watchlist hit.
3. The **BFF** relays `prahari:alerts` as SSE, filtered per connection to the
   caller's org subtree; the console renders them. Health heartbeats travel
   worker → registry → the `camera_current` SQL view (staleness computed at
   read time) → the map.

## 5. Data flow: plate → route (the mandatory case)

`GET /api/v1/routes/{plate}` (BFF, purpose-coded, audited) → correlation's
detection store → ordered hops where each consecutive pair passes a
**spatio-temporal feasibility gate** (haversine distance vs a speed envelope —
deliberately not road-network routing; conservative in the safe direction) →
unreadable-plate gaps bridged only via `appearance_embedding` cosine similarity
*and* feasibility, labelled `bridged`, never silently merged → CSV/PDF export
with per-hop provenance.

## 6. The registry/GIS model

`cameras` carry `external_id` (per catalogue source), location (PostGIS),
lifecycle, `org_id` into an **`ltree` org tree** (`gj.ahmedabad_city.zone_4` —
arbitrary depth, one `path <@ scope` predicate), plus `adapter` and
AES-GCM-encrypted stream credentials for locally-registered analog/DVR units.
Catalogue sync is idempotent and marks vanished cameras `absent` rather than
deleting them — their detections are evidence. `districts`, `dark_zones` and
`nearest` gap queries serve Model 1's coverage analysis; all of it is scoped
by the same org predicate.

## 7. Deliberately not built

- **Model 4 / centralised video** — rejected by arithmetic, §1.
- **A re-ID model** — appearance bridging is a cosine threshold over the
  existing low-dimensional embedding, labelled honestly in output.
- **Road-network routing** — haversine is strictly more permissive than road
  distance, so the gate errs toward keeping a hop, never toward inventing one.
- **An IdP beyond Keycloak's realm file** — `auth.kind=keycloak` ships OIDC
  code+PKCE with server-side exchange (`docs/KEYCLOAK.md`); what remains is
  real org membership in Keycloak and SCIM/federation for the estate.
- **MediaMTX recording for evidence clips** — the request chain and
  `playback` tickets exist; `record: yes` on reconciled paths is the
  follow-on (`docs/EVIDENCE.md`).

## 8. Deployment shape

One Helm chart; `profile: local | gpu` is the only cutover knob (model size,
decode backend, sampling, batch size, KEDA, GPU requests — all values, never
code paths). `infra/terraform/modules/district` is the statewide-rollout
artifact: `ceil(camera_count / streams_per_gpu)` derives node count from the
*measured* figure, which is still an estimate until the Day-4 measurement run
happens — flagged as such everywhere it is used.
