# Org tiers design — hierarchy, RBAC auth, camera onboarding

Referenced from `TODO.md` (new work, opened after Day 3). `docs/PLAN.md` is the *why* for the
whole project; this is the *how* for the three-board split: global (statewide), organization
(one org's estate), local body (sync + self-registration, including analog behind DVRs). Written
before the code, so the reviews in step 3 of `CLAUDE.md`'s working agreement have something to
review against.

**Gate:** a user signed in at any of the three org depths sees exactly the cameras in their
subtree — provably, by asserting absent ids in the response payload, not by hiding rows in the
UI — and the mandatory test case (plate → timestamped route) still passes end to end through an
authenticated, scoped BFF.

---

## 1. What this closes, and what it reverses

| Gap | Closed by |
|---|---|
| No org/tenant entity — `district`/`department`/`owner` are free-text with no hierarchy | §2 — `orgs`, an `ltree` self-referencing tree |
| No authentication anywhere in the system | §3 — `services/bff`, sessions + API keys, one `Principal` |
| `services/bff/` is an empty directory | §3, §4 |
| No audited, purpose-coded evidence access (Day 3's own gap) | §4 |
| Analog/DVR cameras have no onboarding path beyond typing a URL by hand | §5 |

`docs/DAY3-DESIGN.md:259` deliberately scoped out "statewide RBAC hierarchy beyond department +
admin — one cross-department grant mechanism, not a full org chart." This design reverses that
call: a 26-department, 34-district statewide brief is better served by a real hierarchy than a
flat string match, and it is cheaper to build the BFF once, hierarchically, than to build
department-scoping now and generalise it later. The department/admin design is not built anywhere
yet, so there is nothing to migrate away from — this *is* the first BFF, not a rewrite of one.

---

## 2. `services/registry` — the org tree and the scope predicate

### 2.1 Schema — `migrations/005_orgs.sql`

```sql
CREATE EXTENSION IF NOT EXISTS ltree;

CREATE TABLE orgs (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    parent_id  uuid REFERENCES orgs(id),
    path       ltree NOT NULL UNIQUE,   -- gj.ahmedabad_city.zone_4
    kind       text NOT NULL,           -- state | organization | local_body — a label, not structure
    name       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX orgs_path_gist ON orgs USING gist (path);

ALTER TABLE cameras ADD COLUMN org_id          uuid REFERENCES orgs(id);
ALTER TABLE cameras ADD COLUMN adapter         text NOT NULL DEFAULT 'manual';
ALTER TABLE cameras ADD COLUMN stream_username text;
ALTER TABLE cameras ADD COLUMN stream_secret   bytea;
CREATE INDEX cameras_org_idx ON cameras (org_id) WHERE lifecycle = 'active';

DROP VIEW camera_current;
CREATE VIEW camera_current AS
SELECT c.*, -- (identical body to 002_cameras.sql, unchanged)
    ...
```

`ltree` gives arbitrary depth with one predicate — `path <@ $scope` — so "state → district → city
→ zone" needs no schema change and no query rewrite the day a fourth level shows up.

**Why `DROP VIEW` and not `CREATE OR REPLACE`:** `camera_current` (`002_cameras.sql:120`) is
`SELECT c.*` plus four derived columns. Postgres freezes the expanded column list at creation;
inserting columns into `cameras` shifts everything after them, and `CREATE OR REPLACE VIEW`
refuses to reorder columns ("cannot change name of view column"). Migration 002 is checksummed
and must not be touched — `005` drops and recreates the view instead.

### 2.2 Scope is required, not optional

`CameraRepository.list` (`repository.py:126`) today takes `district`/`department` as
*caller-supplied* filters — a client that omits them reads the whole estate. This migration adds
`scope: str` (an `ltree` path) as a **required, non-defaulted** parameter on every read:
`list`, `get`, `get_by_external`, `count`, `health_summary`, and every function in `gaps.py`. A
handler that forgets to pass scope fails to type-check rather than silently returning everything —
the same class of structural guarantee as the `ruff TID251` ban on a bare `import cv2`.

```sql
JOIN orgs o ON o.id = c.org_id
WHERE o.path <@ $scope::ltree
```

`create`/`update` take `org_id` directly, forced from the caller's principal at the BFF layer —
the registry itself does not resolve `Principal`, it only trusts the `org_id` it's given, because
the BFF is the only party allowed to decide what an operator is allowed to write.

### 2.3 Catalogue sync gets a home

`RegistrySettings.sync_default_org_path: str` (default the state root). `sync.py`'s
`upsert_from_catalogue` call passes it as `org_id` **on insert only** — `org_id` joins
`district`/`department`/`owner` in the never-overwritten column set (`repository.py:289`,
`COALESCE(EXCLUDED.x, cameras.x)`), so once a local body reassigns a synced camera to itself, no
future sync moves it back.

### 2.4 Camera credentials

`stream_username`/`stream_secret` (AES-GCM, key from `PRAHARI_CREDENTIAL_KEY`, 256-bit, required
at startup with no default — a missing key must fail loudly, not silently store plaintext).
**The secret is absent from every response model, never masked.** `MediaMTXClient` decrypts it
only when writing a fan-out path (`mediamtx.py`); the registry API never returns it, and
`gaps.cameras_geojson` never selects it.

---

## 3. `services/bff` — identity, sessions, API keys

Built from nothing (today: one `.DS_Store`, not in the uv workspace).

### 3.1 Storage: identity in Postgres, audit chain in SQLite

`docs/DAY3-DESIGN.md:160` puts the audit log in SQLite because it is append-only, single-writer,
and never joined — that reasoning still holds. Identity is the opposite case: `users.org_id` and
`api_keys.org_id` need referential integrity with `orgs`, which `cameras` also references.
Splitting identity across a second database would leave scope references nothing can enforce.
So: **`orgs` + `users` + `sessions` + `api_keys` in the registry's Postgres** (migration `006`,
same checksummed advisory-lock runner as every other migration), **audit chain stays SQLite**.

The BFF connects to that Postgres directly for identity reads/writes, but **never writes
`cameras`** — camera mutations always go through the registry's HTTP API, so validation and
MediaMTX reconciliation stay on one path. This boundary is easy to erode by accident (it looks
free to just `UPDATE cameras` from the BFF once you're already connected) — the cross-review in
§6 checks for it explicitly.

```sql
-- 006_identity.sql
CREATE TABLE users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username text NOT NULL UNIQUE,
    password_hash text NOT NULL,       -- argon2id
    org_id uuid NOT NULL REFERENCES orgs(id),
    role text NOT NULL CHECK (role IN ('viewer','operator','admin')),
    created_at timestamptz NOT NULL DEFAULT now(),
    disabled_at timestamptz
);

CREATE TABLE sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
);

CREATE TABLE api_keys (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    key_hash text NOT NULL UNIQUE,     -- sha256; plaintext shown once, at creation, never again
    org_id uuid NOT NULL REFERENCES orgs(id),
    role text NOT NULL CHECK (role IN ('viewer','operator','admin')),
    label text NOT NULL,
    created_by uuid REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    revoked_at timestamptz
);
```

### 3.2 One `Principal`, two credential types

```
Principal = {subject, org_id, org_path, role, kind: "session" | "api_key"}
```

- `POST /api/v1/auth/login` — argon2 verify, `Set-Cookie: prahari_session=<session id>; HttpOnly;
  Secure; SameSite=Lax`. `POST /api/v1/auth/logout` revokes it. `GET /api/v1/auth/me`.
- `Authorization: Bearer pk_…` — looked up by `sha256(key)`, `last_used_at` touched, resolves to
  the same `Principal` shape.
- Every downstream handler depends on `Principal`, never on the credential type. This is what
  lets all four API-key purposes (local-body registration, an on-prem ONVIF agent, a vendor
  `CameraAdapterService` implementation, internal service-to-service calls) share one table and
  one resolver instead of four bespoke mechanisms.

**Role is held at an org node and applies to its subtree.** `viewer` reads; `operator` reads and
registers/edits cameras within scope; `admin` additionally creates sub-orgs, issues API keys, and
manages users within scope. The "global board" is simply `admin` at the root org — not a special
case anywhere in the code, which is the property that makes three boards one codebase honest.

### 3.3 The registry stops being ambiently readable

Every registry `/api/v1/*` route currently has no auth at all (confirmed — zero references to
auth/token/session in `services/registry`). Add `PRAHARI_INTERNAL_TOKEN`, required on every
registry call, shared only between the BFF, the inference workers (`worker.py:128,153`, which
call the registry directly today with no credential), and correlation. The registry becomes
cluster-internal; the BFF is the only browser-facing ingress. This is a **prerequisite** for org
scoping to mean anything — a scope check in the BFF is decorative if the registry is still
reachable directly.

---

## 4. Purpose codes and the hash-chained audit log

Unchanged in mechanism from `docs/DAY3-DESIGN.md §4.2`, with `department` replaced by `org_path`:

```
entry = {id, actor, org_path, purpose_code, resource, action, occurred_at, prev_hash}
hash  = sha256(canonical_json(entry) + prev_hash)
```

Append-only SQLite on a PVC, fixed genesis `prev_hash`. `X-Purpose-Code` required on every
evidence-adjacent request (a route, a camera detail, an export); its absence is `400`, never a
default. `GET /api/v1/audit/verify` walks the whole chain and reports the first broken link — the
demonstrable "break a link, confirm verification detects it" property `security-privacy-auditor.md`
asks for.

**Cross-org access is a logged exception, not ambient.** A principal requesting a resource outside
their subtree gets `403`, and the denial itself is one audit entry (`action: "denied"`) — the
attempt is evidence too, not just the grant.

SSE (`GET /api/v1/alerts/stream`) and report export (`GET /api/v1/routes/{plate}/export?format=csv|pdf`)
are implemented exactly as `DAY3-DESIGN.md §4.3–4.4` specifies, scoped by `org_path` instead of
`department`. **This is on the mandatory path and ships in this stage regardless of the rest of
the org work** — CSV/PDF export is a literal submission requirement.

---

## 5. Camera onboarding and driver detection

Four modes, ordered cheapest and most load-bearing first; the riskiest is last and severable.

**5a. Adapter class.** `cameras.adapter` (added in §2.1): `gateway | rtsp-direct | onvif | manual`,
naming which `CameraAdapterService` (`adapter.proto`) implementation fronts the camera. Near-free —
makes the Reference Model 3 federation claim concrete per camera instead of per gateway.

**5b. Manual entry.** Already exists — `POST /api/v1/cameras` (`app.py:254`), default
`source="manual"`. Needs only `org_id` (forced from the principal) and the credential fields added.
`mark_absent` is already scoped by `source` (`repository.py:370`), so a locally-registered camera
already survives every catalogue sync without any change here — confirmed in source.

**5c. Endpoint probe — `POST /api/v1/cameras/probe`.** Body `{rtsp_url}`. Connects once, forces
`rtsp_transport=tcp` per the hard invariant, and reports observed transport/codec/resolution and
the stream's *declared* fps — labelled as declared, never as measured, since nothing may derive
timing from it (`CLAUDE.md`). It writes nothing; the operator reviews and then calls `create`.

*SSRF is the actual hazard.* This is a server-side fetch of an operator-supplied URL, and the
legitimate use case (a DVR on a private subnet) rules out a blanket private-IP block. Mitigation:
deny link-local and cloud-metadata ranges specifically (`169.254.0.0/16`, `fd00:ec2::254`), pin
the resolved IP between the check and the connect (no re-resolve-after-check gap), cap timeout and
any redirect following, and require `operator` role plus a purpose code, same as any
evidence-adjacent write.

**5d. Bulk CSV import — `POST /api/v1/cameras/import`.** Same validation as 5b, forced into the
caller's scope, reports per-row failures rather than aborting the batch — this is how a ward
actually onboards an estate of 200 analog cameras nobody typed in one at a time.

**5e. ONVIF discovery — last, severable.** WS-Discovery is LAN multicast; it cannot run centrally,
so it needs a small on-prem agent inside the local body's network, authenticated with an org-scoped
API key, that runs `GetDeviceInformation`/`GetProfiles` and pushes **candidates**, never
auto-registered — an operator approves each one before it becomes a camera. This is the highest-risk
item in the whole plan: a new deployable component, a new protocol, and behaviour that cannot be
verified without real ONVIF hardware on a real LAN. 5a–5d already deliver a complete onboarding
story without it, so if time runs out, this is what gets cut.

---

## 6. How this gets verified

Same loop structure as `DAY3-DESIGN.md §7`.

**Loop 1 — build.** `uv run pytest` and `ruff check` green per stage before the next stage starts.

**Loop 2 — cross-verification, by someone who did not write it.** `security-privacy-auditor`
reviews §3–§5 (scope enforcement is real and not client-side-only, the SSRF mitigation in §5c, the
credential column never appearing in a response, chain integrity, the BFF-never-writes-`cameras`
boundary in §3.1). `geo-registry-engineer` reviews §2 (the view rebuild, the `ltree` predicate,
that `gaps.py`'s KNN queries stay scoped). Four parallel Opus reviewers cost $115 on the Day 2
pass (`first-fix.md §6`) — run these sequentially this time.

**Loop 3 — integration.** `make proto`, `make test`, `make lint`, `make verify` to convergence.

**Gate test — `tests/test_org_tiers_gate.py`**, no cluster or gateway required:
1. Seed `gj` → `gj.ahmedabad_city` → `gj.ahmedabad_city.zone_4`, one user per tier, cameras at each.
2. Zone-4 user lists cameras → only zone-4 cameras; assert the parent org's camera ids are **absent
   from the payload**, not merely unrendered.
3. Org user sees org + zone-4; state user sees all three.
4. Zone-4 user requests a camera outside their subtree → `403` plus one audit entry.
5. Zone-4 operator registers an analog camera with credentials → visible to itself and both
   ancestors; the secret appears in no response body anywhere.
6. A catalogue sync runs → the locally-registered camera is untouched (not marked absent, `org_id`
   unchanged).
7. Break one link in the audit chain → `/api/v1/audit/verify` names it.

Then, separately: the mandatory test case (plate → timestamped route) still passes through the now
authenticated, scoped BFF. If this feature breaks that path, the feature is wrong regardless of
what else it does.

---

## 7. Deliberately not in this design

- No IdP/SAML/OIDC — a `users` table is a seed for one, stated as such in `docs/SECURITY.md`.
- No password reset, email verification, or MFA.
- No per-camera ACLs — scope is org-subtree only, exactly as `DAY3-DESIGN.md` scoped
  department-level access, just generalised to a tree.
- No row-level security in Postgres. Enforcement is the required `scope`/`org_id` argument in the
  repository layer — weaker than RLS, and stated as such rather than implied to be equivalent.

---

## 8. Status (as of 20 Sep)

What is actually in the tree, checked against source rather than this document's intent.

**Landed**

- **Stage 1** — `orgs` as an `ltree` tree (migration `005_orgs.sql`, seed root `gj`),
  `cameras.org_id`/`adapter`/`stream_username`/`stream_secret`, `camera_current` rebuilt,
  and `scope` as a required argument on every read path in `repository.py`/`gaps.py`.
- **Stage 2** — identity in the shared Postgres (migration `006_identity.sql`):
  `users` (argon2id), `sessions`, `api_keys` (sha256-keyed, plaintext shown once),
  `POST /api/v1/auth/login|logout`, `GET /auth/me`, one `Principal` shape for both
  credential types, bootstrap-admin seeding that no-ops once any user exists.
- **Stage 3** — the scoped BFF surface: `org_scope` forced server-side on every
  registry read (`_scoped_params`), purpose-code requirement, the hash-chained SQLite
  audit log including `action="denied"` entries, per-connection scoped SSE alert relay,
  and `GET /api/v1/routes/{plate}[/export]` — the mandatory path, deliberately *not*
  org-filtered (see `app.py`'s comment: redacting hops by org would silently break
  statewide tracing).
- **Stage 4a–4d** — `cameras.adapter` labels; AES-256-GCM stream credentials under
  `PRAHARI_CREDENTIAL_KEY`, absent from every response model; org-scoped camera
  create/update/decommission; the SSRF-hardened probe (pinned-IP connect, blocked
  link-local/metadata ranges); bulk CSV import with per-row failure reporting.
- **Stage 5** — the `web/` console: cookie-gated board, scoped API client via the
  same-origin `/api/bff/*` proxy, MapLibre health map, alert panel, plate trace,
  onboarding and admin panels.

**Pending**

- **5e — ONVIF discovery agent.** Only the `onvif`/`onvif_agent` enum labels exist
  (`models.py` in registry and bff). No agent code, no WS-Discovery — severable by
  design, and it stayed severed.
- **Stage 6 — Helm wiring.** `infra/helm/prahari/templates/services.yaml` already
  renders Deployments/Services for `correlation`, `bff` and `web`, but there is no
  `bffEnv`/`correlationEnv` block (so no `PRAHARI_INTERNAL_TOKEN`,
  `PRAHARI_CREDENTIAL_KEY`, session/bootstrap env, or correlation env reach the
  pods), no PVC for `audit.db`, and no Dockerfiles for the three services. The full
  list is `docs/NEXT-PHASE-PLAN.md` §1.3–1.6; until it lands, `make up` cannot run
  this slice.
- **The internal token is implemented but not armed.** `require_internal_token`
  exists in the registry (`app.py`) and the BFF sends `X-Internal-Token`, but both
  default to empty (= off) and no chart sets them; the inference worker's and
  correlation's registry clients don't send a token at all yet. §3.3's "registry
  stops being ambiently readable" is therefore *aspirational in every current
  deployment* — see `docs/SECURITY.md` for the honest boundary map.
- **Gate test `tests/test_org_tiers_gate.py`** — specified in §6 above but not yet
  in the tree; being added separately.
