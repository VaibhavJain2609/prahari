# PRAHARI — security posture

What this document is **not**: a claim that the system is secure. It is a map
of which boundaries exist, what is enforced on each one *today*, and what is
explicitly deferred — the same honesty standard as the rest of the repo. Where
something is implemented but not yet armed, it says so. The work list for
closing the gaps is `docs/NEXT-PHASE-PLAN.md` §2 (on branch
`docs/next-phase-plan`); this file describes the present, that one describes
the future.

The threats that matter here, in order: **evidence integrity** (a forged or
tampered detection/route is worse than a missed one) and **surveillance abuse**
(every access to camera data attributable to an actor and a purpose).
Perimeter defence is third — this system is interesting to an attacker mostly
for what it *records*.

---

## 1. Trust boundaries

```
browser ──cookie──▶ web (Next.js) ──same-origin proxy──▶ BFF ──▶ registry ──▶ Postgres
                                                         │         │  └─────▶ MediaMTX API :9997
                                                         │         └────────▶ govt gateway (catalogue + feeds)
                                                         ├──▶ correlation ──▶ Redis (prahari:detections)
                                                         └──▶ Redis (prahari:alerts)
inference workers ──gRPC──▶ match-engine ──▶ Redis (prahari:alerts / :detections)
inference workers ──HTTP──▶ registry (assignments, heartbeats)
```

| Boundary | Mechanism | State |
|---|---|---|
| Browser → BFF | argon2id login → `prahari_session` cookie (HttpOnly, SameSite=Lax, `Secure` by default); `Authorization: Bearer pk_…` API keys (sha256-stored) | **Implemented.** `session_cookie_secure` must be `false` only for local plain-HTTP dev — and is currently *not set to false by the chart* (NEXT-PHASE-PLAN §1.4). |
| web → BFF | single `/api/bff/*` proxy; cookie + `X-Purpose-Code` forwarded verbatim | **Implemented.** Browser never sees another origin; CORS n/a. |
| Caller → scoped data | BFF forces `org_scope` from the principal (`_scoped_params`); registry predicates on `ltree path <@ scope`; denials audited | **Implemented** at the BFF↔registry seam — *provided the registry is not reachable except through the BFF*, which today is only true by network topology, not enforcement (next row). |
| Internal callers → registry | `X-Internal-Token` middleware (`registry/app.py`), BFF sends `registry_internal_token` | **Implemented, not armed.** Both default to `""` (= off), no chart sets them, and the inference worker's and correlation's registry clients send no token at all. The registry is ambiently readable on every current deployment. |
| Workers → match-engine | gRPC `MetadataIngestService`, protobuf contract | **Plaintext, unauthenticated.** Any pod that can reach :9001 can inject detections — forged detections become forged route evidence. Fix planned: token-in-metadata, then mTLS (NEXT-PHASE-PLAN §2/S1.7). |
| Services → Postgres | password auth, out-of-band Secret | **Implemented** — but the chart's Postgres Secret re-rolls `randAlphaNum` on every `helm upgrade` (§1.2). |
| Services → Redis | none | **None.** No `requirepass`, no TLS. Any pod can `XADD` a forged `Alert` (reaches consoles over SSE) or forged detections (fabricates route evidence). Planned: ACL/`rediss://` or signed envelopes (§2/S1.5). |
| Cluster → govt gateway | gateway host + password live only in the `prahari-gateway` Secret, mounted solely into the registry | **Implemented** — smallest blast radius by design. `optional: true`, so a missing Secret degrades sync but does not take the registry down. |
| Registry → MediaMTX :9997 | none | **None.** The API that holds credentialed `source` URLs is unauthenticated; must gain auth or a NetworkPolicy (§2/S1.2, S1.4). |

### Known exposures, stated plainly

- Camera read models carry `endpoints` (fan-out *and* upstream gateway URLs)
  to any scoped reader. For catalogue cameras those are unauthenticated
  government-gateway URLs — no MediaMTX auth can protect them once handed out.
  Fix: strip to BFF-mediated access (§2/S1.1).
- `GET /api/v1/streams/paths` is unauthenticated on the registry surface.
- Correlation `/api/v1/routes/{plate}` and the match-engine's HTTP surface have
  no auth of their own; they are protected only by not being browser-reachable.
- No login throttling, no `Origin` check on mutating endpoints, session ids
  stored unhashed — hygiene backlog, §2/S3.

## 2. The audit chain

Design (`bff/audit.py`): append-only SQLite; each entry is
`{actor, org_path, purpose_code, resource, action, occurred_at}` hashed as
`sha256(canonical_json(entry) + prev_hash)`; genesis `prev_hash` is 64 zeros.
`GET /api/v1/audit/verify` (admin) walks the chain and names the first broken
link — the demonstrable "break a link, confirm detection" property.

`X-Purpose-Code` is required (400, never defaulted) on every evidence-adjacent
call: camera detail, route read, route export, probe. Cross-org denials are
themselves entries (`action="denied"`) — the attempt is evidence too.

**Known limits, not hidden:**

- **Single writer.** One `sqlite3` connection, appends dispatched via
  `asyncio.to_thread` — the `SELECT prev_hash` + `INSERT` pair is *not
  serialised*, so concurrent appends can fork the chain and `verify()` will
  then fail under ordinary load, not attack. Fix is an `asyncio.Lock` or a
  writer queue (§1.15). Acceptable today only because nothing has yet run it
  hot.
- **Tail truncation is undetectable.** `verify()` catches edits and mid-chain
  breaks; deleting the *last N rows* verifies clean. Needs a signed head or
  external anchor (§2/S2.5).
- **`audit.db` has no PVC yet** — a pod reschedule loses the log. Stage 6 Helm
  work; single-writer also pins the BFF to 1 replica until then.
- Admin/config actions (user/key/org creation, camera writes) are **not**
  currently audited — only evidence reads are. Backwards for an
  accountability log; §2/S2.3.

## 3. Secrets flow

| Secret | Held by | How it gets there |
|---|---|---|
| Gateway host/password (`PRAHARI_GATEWAY_*`) | registry only | `make gateway-secret` → `kubectl create secret generic prahari-gateway --from-env-file=.env`. Never in values.yaml/tfvars/commits. |
| `PRAHARI_CREDENTIAL_KEY` | registry | AES-256-GCM for `cameras.stream_secret`; **not yet set by the chart** — a credential write without it raises `CredentialKeyError` rather than storing plaintext. |
| `PRAHARI_INTERNAL_TOKEN` | registry + BFF (+ workers, correlation — pending) | Implemented, empty-defaulted, chart doesn't set it. Arming it is §2/S1.3. |
| Session ids / API keys | Postgres | keys stored as sha256; sessions as raw ids (hashing is §2/S3). |
| Postgres password | all services | `prahari-postgres` Secret — see the re-roll bug above. |

Camera stream credentials are encrypted at rest **and** absent from every
response model — the encryption is defence in depth for the database, not the
mechanism that keeps them out of responses.

## 4. DPDP / purpose codes

DPDP Act 2023 alignment is the reason for the purpose-code design: personal
data (a plate trace is about a person) is accessed only for a stated purpose,
and the purpose is *recorded*, not just requested. The audit entry binds
`actor + purpose + resource + time`, which is the accountability primitive the
Act's access-log expectations reduce to. Face-derived data stays separable
from plate data per the hard invariants; nothing face-derived is emitted today.

## 5. Deliberately deferred

Each of these is a real gap, tracked in `docs/NEXT-PHASE-PLAN.md`:

- **NetworkPolicies** — deny-all ingress except named callers (§2/S1.4). Today
  "cluster-internal" is a convention, not a control.
- **mTLS** on internal links — after token-in-metadata; a mesh is
  disproportionate at this scale (§2/S1.7, §7).
- **Audited video access** — MediaMTX JWT tickets minted by the BFF, where
  ticket issuance *is* the audit event; live video currently bypasses the
  audit entirely (§2/S4).
- **Evidence pull** — a signed capability `(camera, range, purpose)` redeemed
  for a clip. Nothing stores edge video; `evidence_ref` is never populated
  (§2/S4).
- **WORM anchoring** — exporting signed audit checkpoints so truncation and
  DB-level tampering are detectable outside the system (§2/S2.5).
- **A real IdP** — SAML/OIDC (Keycloak is the mandated candidate, §3). The
  local users/sessions/API-keys stack is a seed for one, not a claim to be one.
