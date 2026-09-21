# PRAHARI — security posture

What this document is **not**: a claim that the system is secure. It is a map
of which boundaries exist, what is enforced on each one *today*, and what is
explicitly deferred — the same honesty standard as the rest of the repo.

The threats that matter here, in order: **evidence integrity** (a forged or
tampered detection/route is worse than a missed one) and **surveillance abuse**
(every access to camera data attributable to an actor and a purpose).
Perimeter defence is third — this system is interesting to an attacker mostly
for what it *records*.

---

## 1. Trust boundaries

```
browser ──cookie──▶ web (Next.js) ──same-origin proxy──▶ BFF ──▶ registry ──▶ Postgres
                                                         │         │  └─────▶ MediaMTX API :9997 (auth)
                                                         │         └────────▶ govt gateway (catalogue + feeds)
                                                         ├──▶ correlation ──▶ Redis (prahari:detections, requirepass)
                                                         └──▶ Redis (prahari:alerts)
browser ──ticket──▶ MediaMTX WHEP (JWT, scoped to cam-<id>, expiring)
inference workers ──gRPC──▶ match-engine ──▶ Redis (prahari:alerts / :detections)
inference workers ──HTTP──▶ registry (assignments, heartbeats)
```

| Boundary | Mechanism | State |
|---|---|---|
| Browser → BFF | `auth.kind`: `builtin` (argon2id login → `prahari_session` cookie, HttpOnly/SameSite=Lax/Secure-by-default) or `keycloak` (OIDC code+PKCE, server-side exchange mints the same cookie; docs/KEYCLOAK.md). `pk_…` API keys stored sha256 | **Implemented.** `session_cookie_secure` is `false` only in the local profile — the chart sets it. Login is throttled (per-username + per-source sliding window) and timing-equalised; login/logout/denials are audit entries. |
| web → BFF | single `/api/bff/*` proxy; cookie + `X-Purpose-Code` + `Origin`/`X-Forwarded-Host` forwarded; decoded `.`/`..`/`%`/`\` segments rejected | **Implemented.** Browser never sees another origin; the proxy cannot be used to smuggle paths off `/api/v1/`. |
| Caller → scoped data | BFF forces `org_scope` from the principal (`_scoped_params`); registry predicates on `ltree path <@ scope`; denials audited | **Implemented** — and the registry surface is no longer ambiently readable (next row). |
| Internal callers → registry | `X-Internal-Token`, `hmac.compare_digest`, constant-time; each caller sends its own identity and the gate resolves it to a caller name checked against an allowlist (`bff`, `correlation`, `inference`); chart injects per-service keys from `prahari-internal` | **Implemented and armed by the chart** (per-caller `PRAHARI_CALLER_TOKEN_*` envs from keys `bff-token`/`correlation-token`/`inference-token` assemble the `internal_tokens` map; `security.internalSecretRequired: true` in real profiles makes a missing Secret a pod-start failure rather than a silent disarm). **Posture:** isolated per-service identities when `internal_tokens` is configured; shared-token fallback (`internal-token`, the `internal` caller) for compatibility — a Secret that predates the per-service keys keeps working in the old posture until `make internal-secret` tops it up. `/readyz` reports `internal_auth: isolated|shared|open`. |
| Workers → match-engine | gRPC `MetadataIngestService` + `x-internal-token` metadata interceptor resolving to caller identity; allowlist `inference`, `bff` | **Implemented.** A leaked worker token now opens only what `inference` may call. Still plaintext TCP — mTLS deferred (§5). |
| BFF → correlation / match-engine | `X-Internal-Token` on both HTTP surfaces, carrying the BFF's own `bff` identity (`bff-token` key) | **Implemented.** Correlation accepts `bff` only; match-engine accepts `bff` + `inference`. |
| Services → Postgres | password auth, out-of-band Secret | **Implemented** — `prahari-postgres` is generated once with `helm.sh/resource-policy: keep`; it no longer re-rolls on upgrade. |
| Services → Redis | `requirepass` via generated `prahari-redis-auth` Secret; all first-party consumers + the KEDA scaler carry the credential | **Implemented.** No TLS inside the cluster — deferred (§5). |
| Browser → MediaMTX | `POST /api/v1/media/preview-ticket` (auth + `X-Purpose-Code` + org scope) → `video_preview` audit entry → scoped Ed25519 JWT (`mediamtx_permissions: read cam-<id>`, short TTL) | **Implemented** — the audit-before-mint ordering is fail-closed: a failed audit write means no ticket. |
| Cluster → govt gateway | gateway host + password live only in the `prahari-gateway` Secret, mounted solely into the registry | **Implemented** — smallest blast radius by design. `optional: true`, so a missing Secret degrades sync but does not take the registry down. |
| Registry → MediaMTX :9997 | `authMethod: http` — the API itself defers credential checks to the registry's `/api/v1/mediamtx/auth` (`internal:<internal-token>` for control, `worker:<worker-token>` for media pull — **separate secrets**, so a leaked fan-out URL is not an API credential; JWT for tickets); NetworkPolicy restricts the port to the registry | **Implemented.** The API that holds credentialed `source` URLs is no longer open. The auth callback stays unauthenticated by necessity (MediaMTX cannot hold the secret it asks about), so it carries the mitigations it can: constant-time compares, a per-source-IP sliding-window rate limit, sanitized/bounded denial logging, and a fail-closed `worker:` check when `worker-token` is unset. |

### What a browser can no longer see

- `endpoints` is stripped wholesale from every camera payload the BFF returns
  (`_public_camera`) — upstream gateway URLs and credential-bearing fan-out
  URLs never leave the internal plane. The `preview.available` flag is the
  only thing a browser learns: *that* a preview can be minted, not *how*.
- `GET /api/v1/streams/paths` redacts credentials **and drops query strings**
  (DVRs accept `?username=&password=` auth) — scheme/host/port/path only.
- `/readyz` returns `database: error`, never exception text.

### NetworkPolicy

Every service has ingress policy in `templates/networkpolicy.yaml` —
default-deny with named pod selectors per flow (workers→registry,
workers→match-engine gRPC, BFF→match-engine HTTP, →redis, →postgres,
registry→mediamtx api, web→bff). **Honest caveat:** k3s' default flannel does
not enforce NetworkPolicy — on the shipped clusters these are documentation
of intent that becomes real the day a CNI that enforces them is installed.

## 2. The audit chain

Design (`bff/audit.py`): append-only SQLite; each entry is
`{actor, org_path, purpose_code, resource, action, occurred_at}` hashed as
`sha256(canonical_json(entry) + prev_hash)`; genesis `prev_hash` is 64 zeros.
`GET /api/v1/audit/verify` (admin) walks the chain and names the first broken
link; `audit/head` exposes the tip hash for external anchoring; `GET /audit`
lists entries.

`X-Purpose-Code` is required (400, never defaulted) on every evidence-adjacent
call: camera detail, route read, route export, probe, preview ticket.
Denials are themselves entries — the attempt is evidence too. Admin actions
(user/key/org creation, camera writes), login success/failure, logout, and
import scope-denials are all audited. Audit writes **precede** the proxied
read/write and fail closed — a response without its audit row does not
happen; upstream failures are recorded as `*_failed` actions.

**Known limits, not hidden:**

- **Concurrent appends serialise** through an `asyncio.Lock` + write
  serialisation inside `AuditLog` (the fork-under-load bug is fixed and
  covered by a 50-concurrent-append test).
- **Tail truncation is undetectable.** `verify()` catches edits and
  mid-chain breaks; deleting the *last N rows* verifies clean. `audit/head`
  exists so an operator can anchor the tip externally; a signed head or WORM
  export is deferred (§5).
- **`audit.db` lives on its own PVC** (`prahari-audit`) and survives
  reschedules — but has no backup/export path yet, and single-writer pins
  the BFF to 1 replica.
- **Mutation-before-audit ordering**: several mutation handlers still apply
  the state change before their audit append — a failed append can leave a
  committed-but-unrecorded mutation. Reads and ticket-minting are
  audit-first; making every mutation transactional (intent row → outcome) is
  the residual.

## 3. Secrets flow

| Secret | Held by | How it gets there |
|---|---|---|
| Gateway host/password (`PRAHARI_GATEWAY_*`) | registry only | `make gateway-secret` → `kubectl create secret generic prahari-gateway --from-env-file=.env`. All `GatewaySettings` fields map — DIRECT_HOST, RTSP/WHEP ports, VERIFY_TLS included. Never in values.yaml/tfvars/commits. |
| `credential-key` + `internal-token` + `worker-token` + per-service `bff-token`/`correlation-token`/`inference-token` | `prahari-internal` Secret → registry, BFF, workers, correlation, match-engine | `make internal-secret` (idempotent; tops an existing Secret up with only the keys it is missing, so a re-run can neither rotate `credential-key` out from under stored credentials nor churn live tokens). `worker-token` is the MediaMTX reader credential in fan-out URLs — deliberately a different secret from `internal-token`, since it lives inside URLs on every worker pod and rotates independently. Each `<service>-token` is that service's caller identity: it is mounted only into the service that presents it and into the receivers that accept it as a `PRAHARI_*_CALLER_TOKEN_*` env; `internal-token` remains as the shared `internal` compat caller. |
| `prahari-redis-auth` | Redis + every consumer + KEDA trigger | Generated by the chart (`resource-policy: keep`). |
| `prahari-oidc` (`client-secret`) | BFF only | Operator-created when `auth.kind=keycloak`. |
| `prahari-bff-bootstrap` | BFF | `make bff-bootstrap` — seeds the first admin only when the users table is empty; refuses to generate a password silently. |
| Session ids / API keys | Postgres | API keys stored sha256; **sessions stored as sha256** (a stolen DB dump no longer yields live sessions). |
| `PRAHARI_MEDIA_JWT_PRIVATE_KEY` | BFF (sign) + registry (verify via JWKS) | Optional env; falls back to an ephemeral per-boot keypair — restarting the BFF invalidates outstanding tickets (documented trade-off). |

Camera stream credentials are encrypted at rest (`cameras.stream_secret`,
AES-256-GCM under `credential-key`) **and** absent from every response model —
the encryption is defence in depth for the database, not the mechanism that
keeps them out of responses.

## 4. DPDP / purpose codes

DPDP Act 2023 alignment is the reason for the purpose-code design: personal
data (a plate trace is about a person) is accessed only for a stated purpose,
and the purpose is *recorded*, not just requested. The audit entry binds
`actor + purpose + resource + time`, which is the accountability primitive the
Act's access-log expectations reduce to. Face-derived data stays separable
from plate data per the hard invariants; nothing face-derived is emitted today.

## 5. Deliberately deferred

Real gaps, tracked against `docs/NEXT-PHASE-PLAN.md` §5/E:

- **mTLS** on internal links — token-in-metadata shipped; a mesh is
  disproportionate at this scale.
- **Per-pod caller identity** — per-SERVICE internal credentials plus
  per-WORKER secret binding shipped: `internal_tokens` maps caller names to
  distinct tokens and each receiver enforces an allowlist, and the registry
  now mints a secret per `worker_id` on the register that asks for one
  (SHA-256 digest in `workers.secret_hash`, migration 010). A bound
  worker_id must then present it as `X-Worker-Secret` on register,
  assignments and heartbeats — so a stolen `inference-token` can no longer
  claim an active worker's lease, hijack its shard, or spoof heartbeats
  under its name. What is still deferred: the shared token still authorizes
  registering NEW unbound ids (and unbound pre-upgrade workers) until
  `worker_secret_required` is armed; the minted secret is held by the
  worker itself — memory only unless `worker_secret_path` points at a
  per-pod volume, which is deliberately never a shared one — so a lost
  secret on a stable worker_id means an operator clears `secret_hash` to
  rebind; and other inference replicas still share `inference-token`, so
  service-wide compromise still reaches every *unbound* id. SPIFFE/SPIRE
  or per-pod minted identities remain the real fix; the `internal`
  shared-token compat caller also remains — the loadtest presents the
  `inference` identity (`PRAHARI_INFERENCE_TOKEN`), so `internal` survives
  only for ad-hoc ops tooling that has not minted a named key.
- **Evidence pull** — a signed capability `(camera, range, purpose)` redeemed
  for a clip. Preview is solved (audited tickets); the request/ticket chain
  exists and `mediamtx.record` renders recording + a PVC, but recording is
  off in both profiles, nothing fetches the `dvr://` locator, and
  `evidence_ref` is stamped but not auto-resolved into a clip fetch.
- **WORM anchoring** — exporting signed audit checkpoints so tail truncation
  and DB-level tampering are detectable outside the system.
- **Redis TLS** — requirepass shipped; `rediss://` deferred.
- **Distributed login throttling** — the limiter is per-process; behind a
  proxy the IP bucket is deployment-global (separately sized) but still not
  coordinated across replicas.
- **Inference metrics are unauthenticated plaintext** on :9090 — mitigated by
  NetworkPolicy once an enforcing CNI exists; on flannel today it is open.
- **MediaMTX media ports are open to all pods** by deliberate design
  (credentials gate access — `worker:` tokens and tickets — rather than
  network position); a compromised pod holding a minted ticket can only read
  the one `cam-<id>` it names, until expiry.
