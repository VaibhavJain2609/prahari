# Operations

The operator runbook: deploy, secrets, readiness, the audit chain, worker
sharding, alerting, backup. Helm + Terraform in `infra/` are the only source
of truth — no `kubectl apply` of ad-hoc YAML. Where something does not exist
yet, this says so instead of documenting around it.

## Deploy

```bash
make cluster            # k3d cluster + local image registry (local only)
make proto              # protobuf stubs — gitignored, required before builds/tests
make images             # build + push service images into the k3d registry
make internal-secret    # prahari-internal (below) — do this BEFORE up on a real deploy
make gateway-secret     # prahari-gateway from .env — needed for catalogue sync
make bff-bootstrap      # first-login admin — needs the two vars in .env
make up                 # helm upgrade --install, PROFILE=local|gpu (default local)
```

`make up PROFILE=gpu` is the whole cloud cutover — model, decode backend,
sampling, batch size, GPU requests and KEDA all swap as values. gpu-profile
prerequisites the chart does not install: the KEDA CRDs/controller
(`inference.keda.enabled` renders a ScaledObject), the NVIDIA runtime
(`runtimeClassName: nvidia`), a real `global.imageRegistry`, and
`mediamtx.browserWhepBase` pointing at the public ingress (see
`values-gpu.yaml` comments — it is commented out on purpose). The in-chart
Keycloak must never render under gpu; `auth.keycloak.url` points at an
external IdP there (`docs/KEYCLOAK.md`).

`make dev` runs Tilt against the same chart for the inner loop. `make down`
uninstalls; `make nuke` deletes the cluster.

Statewide rollout: `cd infra/terraform/envs/demo && terraform apply` — the
`district` module derives GPU node count as `ceil(camera_count /
streams_per_gpu)`; the default 50 is an **unverified estimate** until the
Day-4 measurement run exists (`docs/SCALE-80K.md` §3–4). Copy
`terraform.tfvars.example`, set `ssh_cidr` — the module rejects `0.0.0.0/0`.

## Secrets

Out-of-band, never in values.yaml/tfvars/commits:

| Secret | Keys | Created by | Needed for |
|---|---|---|---|
| `prahari-internal` | `internal-token`, `credential-key`, optionally `media-jwt-private-key` | `make internal-secret` | `internal-token` arms `X-Internal-Token` on every internal surface; `credential-key` encrypts `cameras.stream_secret`. Create-if-absent on purpose — regenerating rotates the key stored credentials were encrypted under. Optional in-cluster so local boots; **a non-local profile without it has enforcement off — a silent no-op.** |
| `prahari-gateway` | `PRAHARI_GATEWAY_*` (HOST, PASSWORD, SCHEME, DIRECT_HOST, RTSP/WHEP_PORT, VERIFY_TLS) | `make gateway-secret` (reads `.env`) | Catalogue sync + feed URLs. Mounted into **registry only**. Absent → sync degrades, everything else runs. |
| `prahari-bff-bootstrap` | `admin-username`, `admin-password` | `make bff-bootstrap` | Seeds one admin, only while the users table is empty — a no-op afterwards, safe to leave. |
| `prahari-oidc` | `client-secret` | `kubectl create secret generic prahari-oidc --from-literal=client-secret=...` | Only when `auth.kind=keycloak`. Optional so pods boot without it; OIDC login then fails loudly at the token exchange while builtin login still works. |
| `prahari-postgres` | `password` | chart-generated, `resource-policy: keep` — or `postgres.existingSecret` | Postgres auth lives in PGDATA; a rotated Secret under an existing PVC breaks every client permanently. |
| `prahari-redis-auth` | `password` | chart-generated, `keep` — or `redis.existingSecret` | `requirepass`; carried by all consumers + the KEDA trigger. |

## Readiness: what /readyz actually answers

`/healthz` never touches a dependency on any service — a liveness probe that
fails on a Postgres blip turns a recoverable outage into a rolling restart.
`/readyz` is the probe that reports dependency truth:

| Service | What ready means |
|---|---|
| registry `:8000` | Postgres `SELECT 1` succeeds. Failure → 503 `{"database": "error"}` — exception text is logged, never returned. |
| match-engine `:8001` | **503 if the watchlist has 0 entries** — an empty watchlist is a silent total failure otherwise. Otherwise `persistence`: `postgres` | `memory` (no `database_url`) | `degraded` — reported, not gated: history rides a different sink than live alerting on purpose. |
| correlation `:8002` | 503 if the Redis detection consumer is not connected; `persistence`: `postgres` | `in-memory` (a restart loses all route history — said out loud, not implied). 503 if Postgres is configured but unreachable. |
| bff `:8080` | Postgres `SELECT 1`. Same error-shape discipline as registry. |
| web `:3000` | No dedicated route exists — the probe hits `/readyz`, the auth middleware answers a redirect to `/login`, and <400 counts as ready. Ready means "Next.js is serving", nothing more. |
| inference | No HTTP probe at all — workers serve no traffic. Liveness is `exec`: `/tmp/prahari-worker-alive` must be touched within 4× the heartbeat interval, catching the process-alive-every-pump-dead failure an HTTP probe cannot see. |
| mediamtx | TCP probes: readiness on the RTSP listener, liveness on the API port (no guaranteed-200 endpoint exists to GET). |

## Verify the audit chain

```bash
# via the console: /admin → audit viewer → verify
# or directly, as an admin principal:
curl -b prahari_session=... http://localhost:8080/api/v1/audit/verify
# → {"ok": true, "first_broken_entry": null, "head_hash": "...", "row_count": N}
curl -b prahari_session=... http://localhost:8080/api/v1/audit/head   # tip hash for external anchoring
```

`verify` walks `sha256(canonical_json(entry) + prev_hash)` and names the
first broken link. **Known limit:** deleting the *last* N rows verifies
clean — anchor `audit/head` externally if truncation matters
(`docs/SECURITY.md` §2). The log lives on the `prahari-audit` PVC;
single-writer pins the BFF to 1 replica.

## Worker sharding

Workers register with the registry and pull a shard of the estate:
`POST /api/v1/workers/register` (idempotent, doubles as the lease keep-alive)
then `GET /api/v1/assignments?worker_id=...` returns
`(row_number-1) % shard_count` over a stable ordering.

```bash
kubectl exec -n prahari prahari-postgres-0 -- \
  psql -U prahari -d prahari -c \
  "SELECT worker_id, last_seen, shard_index, shard_count FROM workers ORDER BY last_seen DESC;"
```

A worker counts as alive while `last_seen` is within **2×**
`registry.assignment.leaseSeconds` (default 60 s); the row is reaped at 3×.
There is no DELETE — a dead pod's lease expires, which is the only teardown a
crashed container can be relied on to perform. `inference.
assignmentRefreshSeconds` (default 30) must stay well below the lease or
live pods get ejected from the pool and their slices go unpulled. A worker
with `streams_active < streams_assigned` on its `:9090` /metrics is a camera
mid-backoff — that gap is what the exec liveness probe gates on.

## Alerting

**There is none.** No PrometheusRule and no alert definitions exist anywhere
(`docs/OBSERVABILITY.md` "Known gaps" — also: no dashboards, no logs
pipeline, no tracing; registry/bff/web emit no metrics). What exists:
hand-rolled `/metrics` on match-engine (`:8001`), correlation (`:8002`),
inference (`:9090`), mediamtx (`:9998`), scrape annotations and
ServiceMonitors behind `observability.enabled`. match-engine and correlation
gate `/metrics` behind `X-Internal-Token` — a scraper must send the token
from `prahari-internal` or get 401s. The numbers worth alerting on when
rules land: `alert_publish_failures_total`, `heartbeat_failures`,
`detections_pending` growth, `bloom_false_positive_rate` drift.

Manual check meanwhile:

```bash
kubectl port-forward -n prahari svc/prahari-inference-metrics 9090 &  # workers
kubectl port-forward -n prahari svc/prahari-match-engine 8001 &
curl -H "X-Internal-Token: $(kubectl get secret -n prahari prahari-internal \
  -o jsonpath='{.data.internal-token}' | base64 -d)" localhost:8001/metrics
```

## Backup

`make backup` dumps Postgres and pulls the audit log off the cluster into
`backups/<timestamp>/`:

```make
# (in the Makefile) — pg_dump over the pod's local socket, plus audit.db via
# SQLite's online backup API so the copy is consistent rather than a raw cp
# of a live file.
```

What is covered: Postgres (cameras, orgs, users, heartbeats, alerts,
sightings — the system of record) and `audit.db` (the hash chain). What is
deliberately not: Redis streams are MAXLEN-bounded relays — alerts persist
to Postgres *before* the stream publish, so the bus is not the record.
Verify a backup by running `audit/verify` against a restored `audit.db`;
test the Postgres dump with `psql -f` into a scratch database, not by
reading the file. There is no scheduled backup — this is a manual target,
and restores are a manual `psql`/`kubectl cp` in reverse.
