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
