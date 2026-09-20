# Keycloak / OIDC console SSO

`auth.kind: builtin | keycloak` — the same deployment-knob convention as
`bus.kind`. Design context and the recorded caveats: NEXT-PHASE-PLAN.md §3.

`builtin` (the default in **every** profile, including `local`) is the existing
argon2 + `prahari_session` login. `keycloak` adds OIDC Authorization Code +
PKCE against a Keycloak realm. The two are not exclusive at runtime: the OIDC
routes exist only when enabled, builtin login never goes away, and the
bootstrap admin remains the break-glass path during an IdP outage.

## How the flow works

```
browser                web (Next.js)              BFF                    Keycloak
   |                       |                        |                        |
   |-- GET /api/bff/auth/oidc/login --------------->|                        |
   |<-- 302 Location: <issuer>/.../auth?code_challenge&state&nonce           |
   |    Set-Cookie: prahari_oidc_state (signed, httponly, 5min)              |
   |--------------------- login at the IdP ------------------------------->|
   |<-- 302 <redirectBase>/api/bff/auth/oidc/callback?code&state             |
   |-- GET .../callback -------------------------------->|                  |
   |                        |                 POST .../token (code+verifier+secret)
   |                        |                 GET  .../certs (JWKS)          |
   |                        |                 validate id_token: sig, iss,   |
   |                        |                 aud, azp, exp/iat, nonce        |
   |                        |                 resolve or JIT-provision user  |
   |<-- 302 <next>   Set-Cookie: prahari_session (+ prahari_oidc marker)     |
```

The critical decision (from the plan): the callback mints **the same opaque
`prahari_session` cookie** builtin login issues — a server-side session row —
rather than exposing access tokens to the browser. `EventSource` can't set
headers, `proxy.ts` and all cookie plumbing survive unchanged, and
`disabled_at`/`revoked_at` semantics keep working.

### The state cookie

`prahari_oidc_state` is `b64(json).b64(hmac_sha256)` carrying the PKCE
verifier, the CSRF nonce (`state`), the post-login `next` path, and an `iat`.
Signed with the OIDC client secret when one is configured, else a
boot-generated key (local dev — a restart just abandons in-flight logins).
5-minute TTL. Nothing about the flow is persisted server-side, so any replica
can complete it.

### `next` handling

`next` is validated to a same-origin absolute path (`/x` yes, `//x`/absolute
URLs/control chars no) both at login and again at callback — it becomes a
`Location` header, so an unchecked value is an open redirect.

## User resolution: claim mapping, validated never trusted

`preferred_username` (fallback `sub`) identifies the user.

- **Existing user** (matched on `username`): Postgres stays authoritative —
  role and org come from the `users` row. A present `org_path` claim is only
  a consistency check and must equal the DB org path; a mismatch is a 403.
  A disabled account is a 403.
- **New user (JIT)**: requires the `org_path` claim, validated against the
  ltree alphabet `^[a-z0-9_]+(\.[a-z0-9_]+)*`, and it must name an org that
  exists in `orgs` — else 403, fail closed. An IdP claim matching no real org
  is a misconfiguration, not a scope. Role is `realm_access.roles` ∩
  {viewer, operator, admin}, highest wins, default `viewer`. The row is
  created with a random unguessable password — SSO-provisioned users can
  never satisfy builtin password login.

`users`/`sessions` have no `sub`/`auth_via` column (006_identity.sql is
registry-owned). A `prahari_oidc` marker cookie is set at callback so logout
can tell an OIDC-born session apart; it is not a security boundary.

## Logout

`POST /api/v1/auth/oidc/logout` revokes the local session, clears both
cookies, and — when the marker says the session came from OIDC — returns
`{"end_session_url": ...}` pointing at the realm's RP-initiated logout
endpoint with `post_logout_redirect_uri=<redirectBase>`. The console navigates
`window.location` there (a fetch can't follow a 302 into Keycloak's HTML).
Without the RP hop, "Sign out" would silently re-authenticate off the live
IdP session.

## Chart wiring

```yaml
auth:
  kind: builtin            # builtin | keycloak
  redirectBase: ""         # console public origin — REQUIRED when kind=keycloak
  keycloak:
    enabled: false         # in-chart dev IdP (local profile only)
    image: keycloak/keycloak:26.7
    realm: prahari
    url: ""                # external IdP base — gpu profile; empty = in-chart
    publicUrl: http://localhost:8081  # browser-facing base of the in-chart IdP
    clientId: prahari-bff
```

When `kind=keycloak` the BFF gets (all real `BFFSettings.oidc_*` fields):

| env | value |
|---|---|
| `PRAHARI_OIDC_ENABLED` | `true` |
| `PRAHARI_OIDC_ISSUER_URL` | public realm base — `{url\|publicUrl}/realms/{realm}`; expected `iss` and browser-facing endpoint base |
| `PRAHARI_OIDC_INTERNAL_URL` | where the pod calls token/JWKS — `http://prahari-keycloak:8080/realms/prahari` in-chart, else `= issuer` |
| `PRAHARI_OIDC_CLIENT_ID` | `auth.keycloak.clientId` |
| `PRAHARI_OIDC_CLIENT_SECRET` | `prahari-oidc` Secret key `client-secret` (`optional: true`) |
| `PRAHARI_OIDC_REDIRECT_BASE` | `auth.redirectBase` — `required` when kind=keycloak |

The in-chart Deployment renders only when `kind=keycloak` AND
`keycloak.enabled` AND `keycloak.url` is empty. It runs `start-dev
--import-realm` (H2, no TLS, bootstrap `admin`/`admin` — laptop-only), health
on the management port 9000 (`/health/ready`, `/health/live`), realm JSON from
the `prahari-keycloak-realm` ConfigMap, and `KC_HOSTNAME=<publicUrl>` so `iss`
is pinned to the browser-facing base while the BFF reaches the realm over the
Service name.

## Trying it locally

```bash
# 1. the out-of-band client secret — matches the dev placeholder in the
#    imported realm (dev-only-not-a-real-secret)
kubectl create secret generic prahari-oidc \
  --from-literal=client-secret=dev-only-not-a-real-secret

# 2. flip the switch
helm upgrade prahari infra/helm/prahari -f infra/helm/prahari/values-local.yaml \
  --set auth.kind=keycloak --set auth.keycloak.enabled=true

# 3. browser access to the IdP (k3d cluster.yaml maps no host port for it)
kubectl port-forward svc/prahari-keycloak 8081:8080
```

Then bootstrap a user in the admin console (`http://localhost:8081`,
`admin`/`admin`, realm `prahari`):

- create user → set a username + password
- user attribute `org_path` = an existing ltree path, e.g. `gj` or
  `gj.ahmedabad_city.zone_4` (required for JIT provisioning of new users)
- assign realm role `viewer`/`operator`/`admin`

Then browse `http://localhost:3000/api/bff/auth/oidc/login` — or open the BFF
directly at `http://localhost:8080/api/v1/auth/oidc/login` — to run the flow.

## What's deliberately deferred

- **Console SSO UX** (`web/**` — console IA agent): a "Sign in with SSO"
  affordance on `/login` and logout wiring that navigates `end_session_url`.
  Note the BFF proxy route handler currently does not forward a `location`
  header, so 302s must be driven directly at the BFF host or the proxy gains
  `location` forwarding — either is the web agent's call.
- **`users.sub` / `sessions.auth_via` columns**: schema is registry-owned
  (006_identity.sql); today username-match + marker cookie stand in for them.
- **`pk_*` API keys** stay local — the purpose taxonomy is DB semantics
  Keycloak client-credentials doesn't carry. Keycloak service accounts for
  agents are a later option, not this change.
- **Forwarding user JWTs to registry/correlation**: never — `aud` would be
  wrong; service-to-service stays `internal_token` (or Keycloak
  client-credentials later).
- **In-chart Keycloak is not a production IdP** — `start-dev` only. The gpu
  profile points `auth.keycloak.url` at a real instance.
