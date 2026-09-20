# web/ — the PRAHARI console

The operator console for PRAHARI: the one browser-facing surface of the
platform. Everything a user can do — sign in, browse the scoped camera estate
on a map, watch alerts, trace a plate, onboard cameras, administer orgs and
users — goes through the BFF (`services/bff`), never at the registry or
correlation services directly.

## Stack

- **Next.js 16** (App Router) + **React 19** + TypeScript
- **MapLibre GL 6** for the camera health map
- **Tailwind CSS 4**
- No client state library — the BFF is the state; components fetch on mount
  and the alert panel holds one `EventSource`.

## How the browser reaches the backend

Two layers, both intentional:

1. **`src/proxy.ts`** (Next middleware) — a *coarse* gate: no `prahari_session`
   cookie → redirect to `/login`. It checks presence only, never validity; the
   BFF is the only thing that knows whether a session is live, and every page
   calls `api.me()` on load and bounces itself on a 401.
2. **`src/app/api/bff/[...path]/route.ts`** — the single same-origin proxy.
   Every call in `src/lib/api.ts` hits `/api/bff/*`, which forwards method,
   query, body, `cookie`, `authorization` and `x-purpose-code` verbatim to
   `PRAHARI_BFF_URL` (default `http://localhost:8001`). Multi-value
   `Set-Cookie` is preserved via `getSetCookie()`. One origin means CORS never
   comes up, and the browser never learns the BFF's real address.

Scoping is **server-side, always**: the BFF forces the caller's org scope from
their session — this app never decides what a viewer may see, and
`org_scope` is not a parameter a client can set.

## What's on the board

| Panel | File | Backs onto |
|---|---|---|
| Camera health map | `components/CameraMap.tsx` | `GET /cameras/geojson` (polled — health moves on staleness windows, not per event) |
| Alert feed | `components/AlertPanel.tsx` | `GET /alerts/stream` (SSE via `EventSource`) |
| Plate trace | `components/PlateTracePanel.tsx` | `GET /routes/{plate}` + CSV/PDF export (purpose-coded, audited) |
| Onboarding | `components/OnboardingPanel.tsx` | manual create, RTSP probe, bulk CSV import |
| Org/user/key admin | `components/AdminPanel.tsx` | `POST /orgs`, `/auth/users`, `/auth/api-keys` |

One codebase serves all three board depths (state / organization / local
body) — the difference is the signed-in principal's org, not the UI.

**Not built:** the WHEP live preview (`StreamEndpoints.whep_url`) — no panel
pulls video today. When it lands it is preview-only, never an inference
source (hard invariant).

## Run it

```bash
npm install
npm run dev     # http://localhost:3000 → redirects to /login
```

The console needs the BFF running and reachable at `PRAHARI_BFF_URL`. The
full no-cluster recipe — Postgres, registry, BFF with a bootstrap admin, then
this — is in the root `README.md` under "Quick local run". Without
`PRAHARI_REDIS_URL` set on the BFF, the alert panel shows the stream as
unavailable (503) — expected, not a bug.

```bash
npm run build   # production build
npm run lint    # eslint
```
