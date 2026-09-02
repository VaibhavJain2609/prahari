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
Environment shared by every service: how to reach the bus, the database and the
registry. Services must hold no state outside these, so any pod can be killed
and rescheduled at any moment.
*/}}
{{- define "prahari.commonEnv" -}}
- name: PRAHARI_PROFILE
  value: {{ .Values.profile | quote }}
- name: PRAHARI_BUS_KIND
  value: {{ .Values.bus.kind | quote }}
- name: PRAHARI_REDIS_URL
  value: "redis://prahari-redis:6379"
- name: PRAHARI_DATABASE_URL
  value: "postgresql://{{ .Values.postgres.user }}:$(POSTGRES_PASSWORD)@prahari-postgres:5432/{{ .Values.postgres.database }}"
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      name: prahari-postgres
      key: password
- name: PRAHARI_REGISTRY_URL
  value: "http://prahari-registry:{{ .Values.services.registry.port }}"
- name: PRAHARI_AUDIT_ENABLED
  value: {{ .Values.audit.enabled | quote }}
- name: PRAHARI_AUDIT_REQUIRE_PURPOSE
  value: {{ .Values.audit.requirePurposeCode | quote }}
{{- end -}}

{{/*
Registry-only environment.

The registry is the one service that talks to the government gateway, so it is
the one service the credential Secret is mounted into. Every other service
reaches cameras through the registry's catalogue and never needs the password —
which is the point: the blast radius of that credential is one Deployment.

The Secret is created out of band (`kubectl create secret generic
prahari-gateway --from-env-file=.env`) or by Terraform from the cloud secret
store. It is NEVER in values.yaml, and `optional: true` means a cluster without
it still comes up — the registry logs the absence loudly and serves the map,
because a missing credential must not take down camera health as well as sync.
*/}}
{{- define "prahari.registryEnv" -}}
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
{{- if .Values.mediamtx.enabled }}
# The registry writes MediaMTX paths from the catalogue at runtime. The
# ConfigMap ships with `paths:` empty on purpose — a hardcoded path passes
# locally and fails on demo day when ids rotate.
- name: PRAHARI_MEDIAMTX_RECONCILE
  value: "true"
- name: PRAHARI_MEDIAMTX_API_URL
  value: "http://prahari-mediamtx:{{ .Values.mediamtx.apiPort }}"
- name: PRAHARI_MEDIAMTX_PUBLIC_HOST
  value: "prahari-mediamtx"
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

Note what is absent: no gateway credential. The match engine sees plate strings,
never pixels and never the feed, so it has no business holding the password.
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
# Same Redis every other service fans out to (see commonEnv) -- without this,
# MatchSettings.redis_url stays None in every deployed profile (it reads
# PRAHARI_MATCH_REDIS_URL, not the shared PRAHARI_REDIS_URL) and "one schema,
# two transports" silently degrades to "one transport": alerts never leave
# /api/v1/alerts.
- name: PRAHARI_MATCH_REDIS_URL
  value: "redis://prahari-redis:6379"
{{- end -}}
