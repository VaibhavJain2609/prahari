# Observability

What the chart wires for metrics, what it deliberately does not, and what an
operator has to supply. Everything here is **opt-in** via
`observability.enabled` (default `false` in `values.yaml`, on in both profile
overlays) — with it off the chart renders none of this.

## What each service actually exposes

All first-party metrics are hand-rolled plaintext in the Prometheus exposition
shape — no `prometheus_client` dependency anywhere
(`services/*/src/*/metrics.py`). That is a deliberate posture: a handful of
counters readable by `curl`, not a metric registry. Two consequences a scraper
config must live with:

- match-engine emits `# TYPE` headers; **correlation and inference emit bare
  `name value` lines** — no `HELP`/`TYPE`. Prometheus parses them fine (type
  `untyped`); anything that insists on metadata will see less.
- All three are **process-local counters that reset on restart**. Under KEDA
  the inference pods churn by design — rates and `increase()` are the honest
  queries, never absolute values.

| Workload | Endpoint | Port | Auth | Series |
|---|---|---|---|---|
| match-engine | `GET /metrics` | 8001 (`services.matchEngine.port`) | `X-Internal-Token` when `PRAHARI_MATCH_INTERNAL_TOKEN` is armed | `prahari_match_*` |
| correlation | `GET /metrics` | 8002 (`services.correlation.port`) | `X-Internal-Token` when `PRAHARI_CORRELATION_INTERNAL_TOKEN` is armed | `prahari_correlation_*` |
| inference | `GET /metrics` | 9090 (`inference.metricsPort`, `PRAHARI_INGEST_METRICS_PORT`) | none — plaintext stdlib server | `prahari_worker_*` |
| mediamtx | `GET /metrics` | 9998 (`mediamtx.metricsPort`) | none — `metrics` is in `authHTTPExclude` | `mediamtx_*` |
| registry, bff, web | — | — | — | **no metrics endpoint exists** (gap, below) |

**match-engine** (`services/match-engine/.../metrics.py`): counters
`detections_accepted_total`, `detections_rejected_total`,
`bloom_rejected_total`, `candidates_scored_total`, `matches_confirmed_total`,
`matches_probable_total`, `matches_weak_total`, `alerts_emitted_total`,
`dedup_suppressed_total`, `alert_publish_failures_total`,
`detection_publish_failures_total`, `alerts_persisted_total`,
`alert_persist_failures_total`; gauges `watchlist_entries`,
`bloom_false_positive_rate`, `grpc_active_streams`.

**correlation** (`services/correlation/.../metrics.py` + call sites):
counters `detections_consumed`, `detections_dropped_duplicate`,
`detections_dropped_no_timestamp`, `detections_dropped_future_timestamp`,
`detections_dropped_no_field`, `detections_dropped_undecodable`,
`detections_dropped_store_error`, `sightings_persist_failed`,
`registry_lookup_failures`, `route_queries`, `rejected_hops`, `ungated_hops`;
read-time gauges `store_plates`, `store_unplated`, `detections_stream_length`,
`detections_pending` (omitted when the source can't answer rather than
rendered as a lying 0).

**inference** (`services/inference/.../metrics.py`, `worker.py`): counters
`batches_flushed`, `batch_failures`, `detections_sent`, `grpc_failures`,
`heartbeat_failures`; read-time gauges `streams_assigned`, `streams_active`
(the gap between them is a camera mid-backoff — exactly what the exec liveness
probe gates on), `batch_pending_frames`, `pending_frames_dropped`.

**mediamtx**: its own built-in exposition (`metrics: yes` in
`mediamtx-config.yaml`) — paths, sessions, readers, byte counts.

## What the chart wires (`observability.enabled: true`)

- **Pod annotations** — `prometheus.io/scrape`, `prometheus.io/port`,
  `prometheus.io/path: /metrics` on the pod templates of match-engine,
  correlation, mediamtx and inference, when `observability.prometheusScrape`
  is on (default). This is the zero-CRD contract: a plain Prometheus
  `kubernetes_sd` pod-role scrape config or the OTel collector's prometheus
  receiver picks them up; no operator required.
- **`prahari-inference-metrics` Service** — ClusterIP over the inference pods
  on `inference.metricsPort` (`targetPort: metrics`, a named container port).
  The workers have no Service otherwise (they serve no RPC and KEDA owns
  their replica count under the gpu profile). Rendered whenever
  `observability.enabled` and `inference.metricsPort != 0`, so
  `kubectl port-forward svc/prahari-inference-metrics 9090` works for a
  manual scrape even with no collector installed.
- **ServiceMonitors** — only when `observability.serviceMonitor.enabled`.
  Render-gated because the CRDs belong to the Prometheus Operator and a
  cluster without it fails `helm install` on the unknown Kind. One per
  metrics-bearing workload (match-engine/correlation via their `http` port,
  mediamtx and inference via `metrics` ports). `serviceMonitor.labels` are
  merged onto them — kube-prometheus-stack discovers ServiceMonitors by
  label, so set e.g. `{release: prometheus}` to match the stack's
  `serviceMonitorSelector`.
- **NetworkPolicy ingress** — when `networkPolicies.enabled` AND
  `observability.enabled`, each metrics port gets a `from:` of the
  `observability.monitoringNamespace` namespace
  (`kubernetes.io/metadata.name`, default `monitoring`) plus `podSelector: {}`
  (any pod in the release namespace — covers an in-namespace OTel collector
  or the demo dashboard). Inference previously had **no** ingress rule at
  all — the default-deny cut its metrics port too; this is its only inbound
  allow.

## The token caveat — read before wiring a scraper

match-engine and correlation serve `/metrics` on the **same port** as their
`X-Internal-Token`-gated HTTP API (`require_internal_token` middleware —
only `/healthz`/`/readyz` are exempt). When the `prahari-internal` Secret
exists, a bare scrape gets a 401, not metrics. Options, in order of honesty:

1. Send the token on the scrape: OTel collector prometheus receiver supports
   `headers`; ServiceMonitor endpoints support header/bearer fields on
   current operator versions. The token is in `secret/prahari-internal`,
   key `internal-token` — mount it into the scraper, don't copy it.
2. On a profile where the Secret is intentionally absent (local dev), the
   gate is off and scrapes work unauthenticated — same degrade-everything
   convention as the rest of the chart.
3. Exempting `/metrics` from the middleware is a services-code change and
   deliberately NOT made here — an unauthenticated `/metrics` on the admin
   port needs a conversation the chart can't have.

Inference (`:9090`) and MediaMTX (`:9998`) metrics are unauthenticated by
design; the NetworkPolicy is the only boundary on an enforcing CNI.

## What the operator must provide

- A scraper: `kube-prometheus-stack` into `monitoring` (or set
  `observability.monitoringNamespace` to wherever it lands), or an OTel
  collector, or a hand-rolled Prometheus with a pod-annotation kubernetes_sd
  job. The chart ships none — there is no Prometheus in the repo.
- For the ServiceMonitor path: the operator's CRDs installed **before** this
  chart is upgraded with `serviceMonitor.enabled: true`.
- On an enforcing CNI: nothing extra — the NP rules are emitted. On k3s'
  default flannel: the policies are documentation only, scrapes work
  regardless.

## Known gaps — said out loud, not implied

- **registry, bff, web emit no metrics.** No `/metrics` handler exists in
  those codebases, so nothing annotates or monitors them. Registry Postgres
  pool health and BFF audit-chain integrity are exactly the places a scraper
  would earn its keep — that's services work, not chart work.
- **No alerting.** No `PrometheusRule` and no alert definitions exist
  anywhere. The numbers that would page (`alert_publish_failures_total`,
  `heartbeat_failures`, `detections_pending` growth, `bloom_false_positive_rate`
  drift) are scraped but unacted-upon.
- **No dashboards.** No Grafana, no dashboard ConfigMaps. `curl` + a scrape
  explorer is the whole demo path today.
- **No logs pipeline and no tracing.** Structured logs go to stdout and stop
  there; there are no OTel spans in any service.
- **NetworkPolicy is inert on flannel.** k3s' default CNI does not enforce
  it — locally every rule above is documentation. The rules exist so the day
  the cluster runs Cilium/Calico they are already written; verify the probe
  caveat at the top of `networkpolicy.yaml` before relying on them there.
- **mediamtx metrics were open to all before this change**; the NP now
  scopes :9998 to the monitoring namespace + in-namespace pods when
  `observability.enabled`, and preserves the old open rule when it isn't —
  `enabled: false` is a byte-for-byte no-op.
