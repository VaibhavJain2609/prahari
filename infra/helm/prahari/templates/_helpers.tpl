{{- define "prahari.name" -}}
prahari
{{- end -}}

{{- define "prahari.labels" -}}
app.kubernetes.io/part-of: prahari
app.kubernetes.io/managed-by: {{ .Release.Service }}
prahari.gujarat.gov.in/profile: {{ .Values.profile }}
{{- end -}}

{{- define "prahari.selectorLabels" -}}
app.kubernetes.io/part-of: prahari
{{- end -}}

{{/*
Image reference for a first-party service.
Tags are pinned via global.imageTag — never `latest`. A demo that breaks on the
morning of 7 Sep because an upstream tag moved is an unrecoverable loss.
*/}}
{{- define "prahari.image" -}}
{{- printf "%s/prahari-%s:%s" .registry .name .tag -}}
{{- end -}}

{{/*
Name of the Secret carrying the Postgres password. Out of band when
`postgres.existingSecret` is set; the chart-generated Secret otherwise.
*/}}
{{- define "prahari.postgresSecretName" -}}
{{- .Values.postgres.existingSecret | default "prahari-postgres" -}}
{{- end -}}

{{/*
Name of the Secret carrying the Redis password (`key: password`). Same
contract as postgres: out of band via `redis.existingSecret`, else the
chart-generated `prahari-redis-auth` (helm.sh/resource-policy: keep).
*/}}
{{- define "prahari.redisSecretName" -}}
{{- .Values.redis.existingSecret | default "prahari-redis-auth" -}}
{{- end -}}

{{/*
REDIS_PASSWORD plus the `redis://:$(REDIS_PASSWORD)@...` URL pattern, shared
by every bus consumer's env block. The URL references the env var so the
password appears nowhere in rendered YAML. Empty expansion (`optional: true`
secret absent) yields `redis://:@host` — redis-py treats an empty password as
"no AUTH", matching `--requirepass ""` on the server side: both ends degrade
to passwordless together.
*/}}
{{- define "prahari.redisPasswordEnv" -}}
- name: REDIS_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "prahari.redisSecretName" . }}
      key: password
      optional: true
{{- end -}}

{{/*
Environment shared by every service: how to reach the database.

Deliberately minimal. The repo invariant is that every PRAHARI_* name the chart
sets exists as a settings field — so this block carries only what is genuinely
common (Postgres is read by every service that keeps state: registry and bff via
PRAHARI_DATABASE_URL). Per-service env lives in the per-service blocks below;

historical casualties of putting speculative knobs here:
  - PRAHARI_PROFILE / PRAHARI_BUS_KIND — no settings field ever read them. The
    bus is Redis Streams; a Redpanda switch is deferred (see values.yaml).
  - PRAHARI_REGISTRY_URL — every client addresses the registry under its own
    prefix (PRAHARI_INGEST_REGISTRY_URL, PRAHARI_REGISTRY_BASE_URL,
    PRAHARI_CORRELATION_REGISTRY_BASE_URL).
  - PRAHARI_AUDIT_* — the audit log is the BFF's SQLite file, configured in
    bffEnv; there was never an audit subsystem these flags gated.
  - PRAHARI_REDIS_URL — only the BFF's `redis_url` field reads the bare prefix;
    match-engine and correlation take theirs as PRAHARI_MATCH_REDIS_URL /
    PRAHARI_CORRELATION_REDIS_URL. It lives in bffEnv now.
*/}}
{{/*
Database env — only for services that open a Postgres connection (registry
and bff). Emitted into the worker/match-engine/correlation pods it would be a
dead env, which the chart↔settings parity test now fails on.
*/}}
{{- define "prahari.databaseEnv" -}}
# ORDER IS LOAD-BEARING: Kubernetes expands $(VAR) only against env vars defined
# EARLIER in the list. POSTGRES_PASSWORD must precede PRAHARI_DATABASE_URL or
# the DSN arrives literally containing "$(POSTGRES_PASSWORD)" and auth fails.
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "prahari.postgresSecretName" . }}
      key: password
      # Optional only when the in-chart Postgres is off entirely — services that
      # don't touch Postgres must still start in that configuration. When
      # postgres.enabled, a missing Secret SHOULD fail loudly.
      optional: {{ not .Values.postgres.enabled }}
- name: PRAHARI_DATABASE_URL
  value: "postgresql://{{ .Values.postgres.user }}:$(POSTGRES_PASSWORD)@prahari-postgres:5432/{{ .Values.postgres.database }}"
{{- end -}}

{{- define "prahari.commonEnv" -}}
{{- end -}}

{{/*
Out-of-band Secret shared by the internal-token callers. Created once, by hand
or Terraform, never from values.yaml:

  kubectl create secret generic prahari-internal \
    --from-literal=internal-token=$(openssl rand -hex 32) \
    --from-literal=worker-token=$(openssl rand -hex 32) \
    --from-literal=credential-key=$(openssl rand -base64 32)

`security.internalSecretRequired` drives `optional:` on every reference to
this Secret. False (local default) lets pods boot without it — the registry
then treats an empty internal_token as "enforcement off" and warns loudly
(see RegistrySettings.internal_token). True (every real profile) makes a
missing Secret a pod-start failure instead of a silent disarm: without
internal-token the org-scope gate is decorative, without worker-token every
worker stream read is refused, and without credential-key camera stream
credentials cannot be written or read.
*/}}

{{/*
Registry-only environment.

The registry is the one service that talks to the government gateway, so it is
the one service the credential Secret is mounted into. Every other service
reaches cameras through the registry's catalogue and never needs the password —
which is the point: the blast radius of that credential is one Deployment.

The gateway Secret is created out of band (`kubectl create secret generic
prahari-gateway --from-env-file=.env`) or by Terraform from the cloud secret
store. It is NEVER in values.yaml, and `optional: true` means a cluster without
it still comes up — the registry logs the absence loudly and serves the map,
because a missing credential must not take down camera health as well as sync.
*/}}
{{- define "prahari.registryEnv" -}}
{{- include "prahari.databaseEnv" . }}
- name: PRAHARI_CATALOGUE_SOURCE
  value: {{ .Values.registry.catalogueSource | quote }}
- name: PRAHARI_SYNC_ENABLED
  value: {{ .Values.registry.sync.enabled | quote }}
- name: PRAHARI_SYNC_INTERVAL_S
  value: {{ .Values.registry.sync.intervalSeconds | quote }}
- name: PRAHARI_SYNC_ON_STARTUP
  value: {{ .Values.registry.sync.onStartup | quote }}
- name: PRAHARI_HEALTH_STALE_AFTER_S
  value: {{ .Values.registry.health.staleAfterSeconds | quote }}
- name: PRAHARI_HEARTBEAT_RETENTION_DAYS
  value: {{ .Values.registry.health.heartbeatRetentionDays | quote }}
# Worker sharding lease (workers table, /api/v1/workers/register +
# /api/v1/assignments). A worker counts toward shard_count while its last_seen
# is within 2x this; the row is reaped at 3x. Must comfortably exceed
# inference.assignmentRefreshSeconds — a refresh interval longer than the
# lease would eject live pods from the pool and leave their slice unpulled.
- name: PRAHARI_ASSIGNMENT_LEASE_S
  value: {{ .Values.registry.assignment.leaseSeconds | quote }}
# Internal API gate (X-Internal-Token), the AES-256 key for stored camera
# stream credentials, and the MediaMTX reader token embedded in worker
# fan-out URLs — all real RegistrySettings fields. `worker-token` is a
# SEPARATE secret from `internal-token` on purpose: the media credential
# lives inside URLs on every worker pod, so it must not also be the
# internal-API credential, and the two rotate independently. optional is
# driven by security.internalSecretRequired: false for local dev, true in
# real profiles so a missing Secret fails the pod instead of disarming
# every gate silently.
- name: PRAHARI_INTERNAL_TOKEN
  valueFrom:
    secretKeyRef:
      name: prahari-internal
      key: internal-token
      optional: {{ not .Values.security.internalSecretRequired }}
- name: PRAHARI_CREDENTIAL_KEY
  valueFrom:
    secretKeyRef:
      name: prahari-internal
      key: credential-key
      optional: {{ not .Values.security.internalSecretRequired }}
- name: PRAHARI_WORKER_MEDIA_TOKEN
  valueFrom:
    secretKeyRef:
      name: prahari-internal
      key: worker-token
      optional: {{ not .Values.security.internalSecretRequired }}
{{- if .Values.mediamtx.enabled }}
# The registry writes MediaMTX paths from the catalogue at runtime. The
# ConfigMap ships with `paths:` empty on purpose — a hardcoded path passes
# locally and fails on demo day when ids rotate.
- name: PRAHARI_MEDIAMTX_RECONCILE
  value: "true"
- name: PRAHARI_MEDIAMTX_API_URL
  value: "http://prahari-mediamtx:{{ .Values.mediamtx.apiPort }}"
# publicHost feeds the fan-out URLs the registry hands to in-cluster
# consumers — inference workers (worker.py prefers fanout_rtsp_url). It no
# longer feeds browsers: the WHEP URL a browser gets comes from the BFF's
# preview-ticket response (PRAHARI_MEDIA_WHEP_BASE_URL in bffEnv), so this
# host only needs to resolve from inside the cluster.
- name: PRAHARI_MEDIAMTX_PUBLIC_HOST
  value: {{ .Values.mediamtx.publicHost | quote }}
# Where the registry verifies BFF-minted preview tickets (the JWKS the BFF
# publishes). With the BFF disabled this URL simply never resolves — machine
# credentials are checked locally and are unaffected.
- name: PRAHARI_MEDIA_AUTH_JWKS_URL
  value: "http://prahari-bff:{{ .Values.services.bff.port }}/api/v1/media/jwks"
- name: PRAHARI_MEDIAMTX_RTSP_PORT
  value: {{ .Values.mediamtx.rtspPort | quote }}
- name: PRAHARI_MEDIAMTX_HLS_PORT
  value: {{ .Values.mediamtx.hlsPort | quote }}
- name: PRAHARI_MEDIAMTX_WHEP_PORT
  value: {{ .Values.mediamtx.whepPort | quote }}
{{- else }}
- name: PRAHARI_MEDIAMTX_RECONCILE
  value: "false"
{{- end }}
- name: PRAHARI_GATEWAY_HOST
  valueFrom:
    secretKeyRef:
      name: {{ .Values.registry.gatewaySecret }}
      key: PRAHARI_GATEWAY_HOST
      optional: true
- name: PRAHARI_GATEWAY_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.registry.gatewaySecret }}
      key: PRAHARI_GATEWAY_PASSWORD
      optional: true
- name: PRAHARI_GATEWAY_SCHEME
  valueFrom:
    secretKeyRef:
      name: {{ .Values.registry.gatewaySecret }}
      key: PRAHARI_GATEWAY_SCHEME
      optional: true
# The remaining GatewaySettings fields — a gateway whose media hostname or
# ports differ from the catalogue host (DIRECT_HOST) or nonstandard RTSP/WHEP
# ports silently produced URLs pointing at the wrong place without these.
- name: PRAHARI_GATEWAY_DIRECT_HOST
  valueFrom:
    secretKeyRef:
      name: {{ .Values.registry.gatewaySecret }}
      key: PRAHARI_GATEWAY_DIRECT_HOST
      optional: true
- name: PRAHARI_GATEWAY_RTSP_PORT
  valueFrom:
    secretKeyRef:
      name: {{ .Values.registry.gatewaySecret }}
      key: PRAHARI_GATEWAY_RTSP_PORT
      optional: true
- name: PRAHARI_GATEWAY_WHEP_PORT
  valueFrom:
    secretKeyRef:
      name: {{ .Values.registry.gatewaySecret }}
      key: PRAHARI_GATEWAY_WHEP_PORT
      optional: true
- name: PRAHARI_GATEWAY_VERIFY_TLS
  valueFrom:
    secretKeyRef:
      name: {{ .Values.registry.gatewaySecret }}
      key: PRAHARI_GATEWAY_VERIFY_TLS
      optional: true
{{- end -}}

{{/*
Match-engine-only environment.

PRAHARI_MATCH_* is the env_prefix of MatchSettings. As everywhere else, a name
the chart sets and the code never reads is a profile switch that silently does
not switch — `services/match-engine/tests/test_match_settings.py` asserts
these map to real fields, both directions: it also fails if a MatchSettings
field is neither chart-exposed nor named in that test's explicit
deliberately-internal allowlist (M3 found the reverse direction matters:
`PRAHARI_MATCH_REDIS_URL` was missing here and alerts silently never reached
the shared Redis bus in any deployed profile).

PRAHARI_MATCH_INTERNAL_TOKEN gates the HTTP admin surface AND the
MetadataIngestService gRPC port (the InternalTokenInterceptor). Same shared
Secret key as the registry's gate — every service reads the same value so a
rotation is one Secret update, not five.

Note what else is absent: no gateway credential. The match engine sees plate
strings, never pixels and never the feed, so it has no business holding the
password.
*/}}
{{- define "prahari.matchEngineEnv" -}}
- name: PRAHARI_MATCH_GRPC_PORT
  value: {{ .Values.services.matchEngine.grpcPort | quote }}
- name: PRAHARI_MATCH_HTTP_PORT
  value: {{ .Values.services.matchEngine.port | quote }}
- name: PRAHARI_MATCH_WATCHLIST_DIR
  value: {{ .Values.matchEngine.watchlistPath | quote }}
# Below this, a hit is not surfaced at all. Above the confirm threshold it is
# actionable. The band between them is the "worth a look" tier the console shows
# differently — tuning these is an accuracy decision, so they are values.
- name: PRAHARI_MATCH_WEAK_SCORE
  value: {{ .Values.matchEngine.minScore | quote }}
- name: PRAHARI_MATCH_CONFIRMED_SCORE
  value: {{ .Values.matchEngine.confirmScore | quote }}
# One alert per vehicle per camera per bucket. A vehicle in frame for 8 s at
# 3 fps is one alert, not 24; unbucketed alerting makes the console useless
# within a minute.
- name: PRAHARI_MATCH_DEDUP_BUCKET_S
  value: {{ .Values.matchEngine.dedupBucketSeconds | quote }}
# Same Redis every other service fans out to -- without this,
# MatchSettings.redis_url stays None in every deployed profile (it reads
# PRAHARI_MATCH_REDIS_URL, not a shared PRAHARI_REDIS_URL) and "one schema,
# two transports" silently degrades to "one transport": alerts never leave
# /api/v1/alerts.
# REDIS_PASSWORD must be declared BEFORE the URL that expands it — Kubernetes
# only resolves $(VAR) against earlier entries in the same env list.
{{ include "prahari.redisPasswordEnv" . }}
- name: PRAHARI_MATCH_REDIS_URL
  value: "redis://:$(REDIS_PASSWORD)@prahari-redis:6379"
# Alert persistence: history survives restarts; unset degrades to memory-only
# and /readyz reports it. ORDER: POSTGRES_PASSWORD precedes the DSN — same
# $(VAR) expansion rule as REDIS_PASSWORD above.
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "prahari.postgresSecretName" . }}
      key: password
      optional: {{ not .Values.postgres.enabled }}
- name: PRAHARI_MATCH_DATABASE_URL
  value: "postgresql://{{ .Values.postgres.user }}:$(POSTGRES_PASSWORD)@prahari-postgres:5432/{{ .Values.postgres.database }}"
- name: PRAHARI_MATCH_INTERNAL_TOKEN
  valueFrom:
    secretKeyRef:
      name: prahari-internal
      key: internal-token
      optional: {{ not .Values.security.internalSecretRequired }}
{{- end -}}

{{/*
Correlation-only environment (PRAHARI_CORRELATION_* = CorrelationSettings).

redis_url unset means "the detection consumer never starts" and /readyz says so
honestly — so it is set here, pointed at the same Redis the match engine
publishes `prahari:detections` on. database_url unset means "sightings are
memory-only" and /readyz reports `persistence: in-memory` — so it is also set
here, pointed at the same Postgres database the registry uses.

Two token fields, same Secret key: INTERNAL_TOKEN gates this service's own
/api/* (route reconstruction is surveillance capability — it must not be
callable by any pod that can reach the Service), and REGISTRY_INTERNAL_TOKEN
is what it sends to a gated registry for camera-location lookups.
*/}}
{{- define "prahari.correlationEnv" -}}
- name: PRAHARI_CORRELATION_HTTP_PORT
  value: {{ .Values.services.correlation.port | quote }}
# REDIS_PASSWORD before the URL — see matchEngineEnv for the expansion rule.
{{ include "prahari.redisPasswordEnv" . }}
- name: PRAHARI_CORRELATION_REDIS_URL
  value: "redis://:$(REDIS_PASSWORD)@prahari-redis:6379"
# Correlation persists sightings to the SAME Postgres + database the registry
# uses — the `correlation_` table prefix is the ownership boundary, a second
# database adds nothing an operator wants to babysit. CorrelationSettings reads
# the service-prefixed name (its env_prefix is PRAHARI_CORRELATION_), so this
# DSN is emitted inline rather than via the shared databaseEnv helper, which
# would emit the bare PRAHARI_DATABASE_URL nothing here reads. POSTGRES_PASSWORD
# is declared BEFORE the URL that expands it — $(VAR) only resolves against
# earlier entries in the env list.
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "prahari.postgresSecretName" . }}
      key: password
      optional: {{ not .Values.postgres.enabled }}
- name: PRAHARI_CORRELATION_DATABASE_URL
  value: "postgresql://{{ .Values.postgres.user }}:$(POSTGRES_PASSWORD)@prahari-postgres:5432/{{ .Values.postgres.database }}"
- name: PRAHARI_CORRELATION_REGISTRY_BASE_URL
  value: "http://prahari-registry:{{ .Values.services.registry.port }}"
- name: PRAHARI_CORRELATION_INTERNAL_TOKEN
  valueFrom:
    secretKeyRef:
      name: prahari-internal
      key: internal-token
      optional: {{ not .Values.security.internalSecretRequired }}
- name: PRAHARI_CORRELATION_REGISTRY_INTERNAL_TOKEN
  valueFrom:
    secretKeyRef:
      name: prahari-internal
      key: internal-token
      optional: {{ not .Values.security.internalSecretRequired }}
{{- end -}}

{{/*
BFF-only environment (PRAHARI_* = BFFSettings).

The BFF is the service the browser's data flows through: it proxies the
registry, relays alerts off Redis Streams, and owns the hash-chained audit log
— which is why it is the one Deployment with a persistent volume.
*/}}
{{- define "prahari.bffEnv" -}}
{{- include "prahari.databaseEnv" . }}
- name: PRAHARI_REGISTRY_BASE_URL
  value: "http://prahari-registry:{{ .Values.services.registry.port }}"
- name: PRAHARI_CORRELATION_BASE_URL
  value: "http://prahari-correlation:{{ .Values.services.correlation.port }}"
# X-Internal-Token the BFF sends the registry; must match PRAHARI_INTERNAL_TOKEN
# there. Same Secret, same key, same optional-local rule.
- name: PRAHARI_REGISTRY_INTERNAL_TOKEN
  valueFrom:
    secretKeyRef:
      name: prahari-internal
      key: internal-token
      optional: {{ not .Values.security.internalSecretRequired }}
# The match engine's HTTP admin + gRPC are gated by PRAHARI_MATCH_INTERNAL_TOKEN
# on its side; the BFF sends the same shared value (BFFSettings.internal_token).
- name: PRAHARI_INTERNAL_TOKEN
  valueFrom:
    secretKeyRef:
      name: prahari-internal
      key: internal-token
      optional: {{ not .Values.security.internalSecretRequired }}
# Watchlist summary/reload + alert history proxy targets.
- name: PRAHARI_MATCH_ENGINE_BASE_URL
  value: "http://prahari-match-engine:{{ .Values.services.matchEngine.port }}"
# The SSE alert relay reads the `prahari:alerts` stream. BFFSettings is the only
# settings class that reads the bare PRAHARI_REDIS_URL — everyone else's Redis
# env is service-prefixed — which is why it lives here and not in commonEnv.
# REDIS_PASSWORD before the URL — see matchEngineEnv for the expansion rule.
{{ include "prahari.redisPasswordEnv" . }}
- name: PRAHARI_REDIS_URL
  value: "redis://:$(REDIS_PASSWORD)@prahari-redis:6379"
# Append-only, hash-chained, single-writer — deliberately a SQLite file on the
# prahari-audit PVC, not a table in the shared Postgres. There is no "audit off"
# switch in any profile; the old PRAHARI_AUDIT_* flags gated nothing.
- name: PRAHARI_AUDIT_DB_PATH
  value: "/var/lib/prahari/audit/audit.db"
# Media preview tickets (BFFSettings.media_*). The signing key rides in the
# existing prahari-internal Secret under `media-jwt-private-key`. Stays
# optional even under security.internalSecretRequired: the fallback is an
# ephemeral per-boot keypair — a documented graceful degradation (a restart
# invalidates outstanding tickets, which are ~60s lived), not a gate going
# silently open the way an empty internal_token would be.
- name: PRAHARI_MEDIA_JWT_PRIVATE_KEY
  valueFrom:
    secretKeyRef:
      name: prahari-internal
      key: media-jwt-private-key
      optional: true
# The WHEP URL handed to browsers is the one the BROWSER can reach — the k3d
# port map locally, the ingress host in the cloud. In-cluster mediamtx access
# (workers, the auth callback) never uses it.
- name: PRAHARI_MEDIA_WHEP_BASE_URL
  value: {{ .Values.mediamtx.browserWhepBase | quote }}
# Secure cookies only where TLS terminates. Local k3d serves plain HTTP; every
# other profile must have TLS in front or sessions ship in the clear.
- name: PRAHARI_SESSION_COOKIE_SECURE
  value: {{ eq .Values.profile "local" | ternary "false" "true" | quote }}
# First-user seeding: created only when the users table is empty. Out of band,
# never in values:
#   kubectl create secret generic prahari-bff-bootstrap \
#     --from-literal=admin-username=... --from-literal=admin-password=...
- name: PRAHARI_BOOTSTRAP_ADMIN_USERNAME
  valueFrom:
    secretKeyRef:
      name: prahari-bff-bootstrap
      key: admin-username
      optional: true
- name: PRAHARI_BOOTSTRAP_ADMIN_PASSWORD
  valueFrom:
    secretKeyRef:
      name: prahari-bff-bootstrap
      key: admin-password
      optional: true
{{- if eq .Values.auth.kind "keycloak" }}
# --- oidc: emitted only when auth.kind=keycloak -----------------------------
# All BFFSettings.oidc_* fields. The ISSUER is the public realm base — what the
# browser navigates to for authorize/logout AND what `iss` must equal
# (KC_HOSTNAME pins it). INTERNAL is where the pod's token exchange and JWKS
# fetch actually go: the in-chart Service name locally, the same external URL
# when auth.keycloak.url is set. The client secret is out of band, same pattern
# as prahari-gateway:
#   kubectl create secret generic prahari-oidc --from-literal=client-secret=...
# optional: true so the pods still boot before the Secret exists — OIDC login
# then fails at the token exchange, loudly, while builtin login is unaffected.
- name: PRAHARI_OIDC_ENABLED
  value: "true"
- name: PRAHARI_OIDC_ISSUER_URL
  value: {{ include "prahari.oidcIssuer" . | quote }}
- name: PRAHARI_OIDC_INTERNAL_URL
  value: {{ include "prahari.oidcInternalIssuer" . | quote }}
- name: PRAHARI_OIDC_CLIENT_ID
  value: {{ .Values.auth.keycloak.clientId | quote }}
- name: PRAHARI_OIDC_CLIENT_SECRET
  valueFrom:
    secretKeyRef:
      name: prahari-oidc
      key: client-secret
      optional: true
- name: PRAHARI_OIDC_REDIRECT_BASE
  value: {{ required "auth.redirectBase must be set when auth.kind=keycloak" .Values.auth.redirectBase | quote }}
{{- end }}
{{- end -}}

{{/*
Web-only environment. The Next.js route handlers proxy /api/bff/* to the BFF
in-cluster (web/src/app/api/bff/[...path]/route.ts reads PRAHARI_BFF_URL).
*/}}
{{- define "prahari.webEnv" -}}
- name: PRAHARI_BFF_URL
  value: "http://prahari-bff:{{ .Values.services.bff.port }}"
{{- end -}}

{{/*
oidc: the PUBLIC realm issuer — what the browser navigates to (authorize,
RP-initiated logout) and what `iss` must equal. Keycloak pins iss to
KC_HOSTNAME, which the keycloak.yaml deployment sets to auth.keycloak.publicUrl;
an external IdP (auth.keycloak.url) is already its own public name.
*/}}
{{- define "prahari.oidcIssuer" -}}
{{- if .Values.auth.keycloak.url -}}
{{- printf "%s/realms/%s" (.Values.auth.keycloak.url | trimSuffix "/") .Values.auth.keycloak.realm -}}
{{- else -}}
{{- printf "%s/realms/%s" (.Values.auth.keycloak.publicUrl | trimSuffix "/") .Values.auth.keycloak.realm -}}
{{- end -}}
{{- end -}}

{{/*
oidc: the INTERNAL realm base — where the BFF pod's token exchange and JWKS
fetch go. Equal to the public issuer when one URL serves both (external IdP);
the in-chart Service name for the local dev deployment.
*/}}
{{- define "prahari.oidcInternalIssuer" -}}
{{- if .Values.auth.keycloak.url -}}
{{- printf "%s/realms/%s" (.Values.auth.keycloak.url | trimSuffix "/") .Values.auth.keycloak.realm -}}
{{- else -}}
{{- printf "http://prahari-keycloak:8080/realms/%s" .Values.auth.keycloak.realm -}}
{{- end -}}
{{- end -}}
