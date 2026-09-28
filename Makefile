.DEFAULT_GOAL := help
CLUSTER  := prahari
NAMESPACE := prahari
CHART    := infra/helm/prahari
PROFILE  ?= local
LTARGS   ?= selftest

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# --- contracts -------------------------------------------------------------

.PHONY: proto
proto: ## Regenerate protobuf stubs from proto/
	# Python stubs land in packages/prahari-proto/ (an installable workspace
	# package, because generated protobuf imports are absolute). The TS target
	# was dropped — the web console consumes REST/SSE, not generated stubs.
	# Stubs are gitignored — the contract is the
	# .proto file. A fresh clone must run this before the imports resolve.
	cd proto && buf generate

.PHONY: proto-lint
proto-lint: ## Lint the protobuf contract
	cd proto && buf lint

.PHONY: proto-breaking
proto-breaking: ## Check for breaking contract changes against main
	# --git-common-dir, not ../.git: in a linked worktree .git is a FILE, and
	# buf resolves the git input relative to the proto/ cwd — the common dir
	# is the real repository either way.
	cd proto && buf breaking --against "$(shell git rev-parse --path-format=absolute --git-common-dir)#branch=main,subdir=proto"

# --- local cluster ---------------------------------------------------------

.PHONY: cluster
cluster: ## Create the local k3d cluster
	k3d cluster create --config infra/k3d/cluster.yaml

.PHONY: up
up: ## Install the platform locally (profile=local)
	helm upgrade --install prahari $(CHART) \
	  --namespace $(NAMESPACE) --create-namespace \
	  --values $(CHART)/values-$(PROFILE).yaml \
	  --set profile=$(PROFILE) \
	  --wait --timeout 5m

.PHONY: dev
dev: ## Inner dev loop (tilt up)
	tilt up

.PHONY: images
images: proto ## Build the service images into the k3d registry
	# Built from the workspace root: every service depends on
	# packages/prahari-common, which a narrower build context would exclude.
	# `proto` first because the stubs are gitignored — on a fresh clone the
	# prahari-proto wheel would otherwise install empty and every consumer
	# ImportErrors on `prahari.v1` at runtime, in an image that built green.
	docker build -f services/registry/Dockerfile     -t localhost:5555/prahari-registry:dev     .
	docker build -f services/inference/Dockerfile    -t localhost:5555/prahari-inference:dev    .
	docker build -f services/match-engine/Dockerfile -t localhost:5555/prahari-match-engine:dev .
	docker build -f services/correlation/Dockerfile  -t localhost:5555/prahari-correlation:dev  .
	docker build -f services/bff/Dockerfile          -t localhost:5555/prahari-bff:dev          .
	docker build -f web/Dockerfile                   -t localhost:5555/prahari-web:dev          .
	docker push localhost:5555/prahari-registry:dev
	docker push localhost:5555/prahari-inference:dev
	docker push localhost:5555/prahari-match-engine:dev
	docker push localhost:5555/prahari-correlation:dev
	docker push localhost:5555/prahari-bff:dev
	docker push localhost:5555/prahari-web:dev

.PHONY: gateway-secret
gateway-secret: ## Load .env into the cluster as the gateway credential Secret
	# The credential never enters values.yaml, tfvars or a commit. This reads
	# the gitignored .env and nothing else.
	@test -f .env || { echo "no .env — copy .env.example and fill it in"; exit 1; }
	kubectl create secret generic prahari-gateway \
	  --namespace $(NAMESPACE) --from-env-file=.env \
	  --dry-run=client -o yaml | kubectl apply -f -

.PHONY: bff-bootstrap
bff-bootstrap: ## Create the prahari-bff-bootstrap Secret (first-login admin) from .env
	# Without this Secret there is no first login on a fresh cluster — the BFF
	# seeds one admin only when the users table is empty. Reads
	# PRAHARI_BOOTSTRAP_ADMIN_USERNAME/_PASSWORD from .env; refuses to generate
	# a random password silently, because an admin password nobody knows is
	# worse than a loud failure here.
	@if kubectl get secret prahari-bff-bootstrap --namespace $(NAMESPACE) >/dev/null 2>&1; then \
	  echo "prahari-bff-bootstrap already exists — leaving it alone"; \
	else \
	  test ! -f .env || { set -a; . ./.env; set +a; }; \
	  test -n "$${PRAHARI_BOOTSTRAP_ADMIN_USERNAME}" && test -n "$${PRAHARI_BOOTSTRAP_ADMIN_PASSWORD}" || { \
	    echo "set PRAHARI_BOOTSTRAP_ADMIN_USERNAME and PRAHARI_BOOTSTRAP_ADMIN_PASSWORD in .env first"; exit 1; }; \
	  kubectl create secret generic prahari-bff-bootstrap --namespace $(NAMESPACE) \
	    --from-literal=admin-username="$$PRAHARI_BOOTSTRAP_ADMIN_USERNAME" \
	    --from-literal=admin-password="$$PRAHARI_BOOTSTRAP_ADMIN_PASSWORD"; \
	fi

.PHONY: internal-secret
internal-secret: ## Create or top-up the prahari-internal Secret (per-service tokens + media token + credential key)
	# The keys every real deployment needs:
	#   internal-token    — the legacy shared credential. Arms every internal
	#                       gate in "shared" mode, and on an isolated gate
	#                       resolves to the `internal` compat caller accepted
	#                       everywhere (loadtest, not-yet-migrated callers).
	#   bff-token         — the BFF's caller identity; registry, match-engine
	#                       and correlation all accept `bff`.
	#   correlation-token — correlation's caller identity (registry accepts
	#                       `correlation` for camera-location lookups).
	#   inference-token   — the ingest workers' caller identity (registry +
	#                       match-engine gRPC accept `inference`).
	#   worker-token      — the MediaMTX reader credential embedded in worker
	#                       fan-out URLs as `worker:<token>` userinfo. SEPARATE
	#                       from internal-token on purpose: it lives inside URLs
	#                       on every inference pod, so it must not also unlock
	#                       the internal API, and it rotates independently.
	#   credential-key    — 32-byte AES-256 key, urlsafe-base64, encrypting
	#                       cameras.stream_secret (registry crypto.py).
	# Production should supply real values via INTERNAL_TOKEN, BFF_TOKEN,
	# CORRELATION_TOKEN, INFERENCE_TOKEN, WORKER_MEDIA_TOKEN and CREDENTIAL_KEY
	# in .env (or a secrets manager); the generated fallbacks exist so a local
	# cluster works out of the box.
	#
	# An existing Secret is TOPPED UP, never rewritten: only keys it is
	# missing are merged in, so re-running can never rotate credential-key
	# out from under cameras.stream_secret or churn a live token. Upgrading a
	# deployment that predates per-service tokens therefore needs no Secret
	# surgery — just re-run this target.
	@test ! -f .env || { set -a; . ./.env; set +a; }; \
	tok() { \
	  case "$$1" in \
	    internal-token)    printf %s "$${INTERNAL_TOKEN:-$$(openssl rand -hex 32)}" ;; \
	    worker-token)      printf %s "$${WORKER_MEDIA_TOKEN:-$$(openssl rand -hex 32)}" ;; \
	    credential-key)    printf %s "$${CREDENTIAL_KEY:-$$(python3 -c 'import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')}" ;; \
	    bff-token)         printf %s "$${BFF_TOKEN:-$$(openssl rand -hex 32)}" ;; \
	    correlation-token) printf %s "$${CORRELATION_TOKEN:-$$(openssl rand -hex 32)}" ;; \
	    inference-token)   printf %s "$${INFERENCE_TOKEN:-$$(openssl rand -hex 32)}" ;; \
	  esac; \
	}; \
	KEYS="internal-token worker-token credential-key bff-token correlation-token inference-token"; \
	if kubectl get secret prahari-internal --namespace $(NAMESPACE) >/dev/null 2>&1; then \
	  patch=""; \
	  for key in $$KEYS; do \
	    if ! kubectl get secret prahari-internal --namespace $(NAMESPACE) \
	        -o "jsonpath={.data.$$key}" | grep -q .; then \
	      patch="$$patch\"$$key\":\"$$(tok $$key | base64 | tr -d '\n')\","; \
	    fi; \
	  done; \
	  if [ -z "$$patch" ]; then \
	    echo "prahari-internal already exists with all keys — leaving it alone"; \
	  else \
	    kubectl patch secret prahari-internal --namespace $(NAMESPACE) \
	      --type merge -p "{\"data\":{$${patch%,}}}" >/dev/null && \
	    echo "topped up prahari-internal — added only the missing keys (existing values untouched)"; \
	  fi; \
	else \
	  kubectl create secret generic prahari-internal --namespace $(NAMESPACE) \
	    --from-literal=internal-token="$$(tok internal-token)" \
	    --from-literal=worker-token="$$(tok worker-token)" \
	    --from-literal=credential-key="$$(tok credential-key)" \
	    --from-literal=bff-token="$$(tok bff-token)" \
	    --from-literal=correlation-token="$$(tok correlation-token)" \
	    --from-literal=inference-token="$$(tok inference-token)" \
	    --dry-run=client -o yaml | kubectl apply -f -; \
	fi

BACKUP_DIR ?= backups/$(shell date +%Y%m%d-%H%M%S)

.PHONY: backup
backup: ## Dump Postgres + the audit log into $(BACKUP_DIR)
	# What this covers: Postgres is the system of record (cameras, orgs,
	# users, alerts, sightings); audit.db is the hash chain. Redis is not
	# backed up — the streams are MAXLEN-bounded relays, and alerts hit
	# Postgres before the stream publish. See docs/OPERATIONS.md.
	@mkdir -p $(BACKUP_DIR)
	# pg_dump over the pod's local socket — no password needed there.
	# `prahari`/`prahari` are postgres.user/postgres.database in values.yaml;
	# override them there and here together if they ever diverge.
	kubectl exec -n $(NAMESPACE) prahari-postgres-0 -- \
	  pg_dump -U prahari prahari > $(BACKUP_DIR)/prahari.sql
	# audit.db is a live SQLite file; a raw kubectl cp can catch a mid-write
	# page. sqlite3's online backup API copies it consistently first, and the
	# bff image has python on PATH. BFF is single-replica (single-writer), so
	# deploy/prahari-bff resolves to the only pod.
	kubectl exec -n $(NAMESPACE) deploy/prahari-bff -- python -c \
	  "import sqlite3; s=sqlite3.connect('/var/lib/prahari/audit/audit.db'); d=sqlite3.connect('/tmp/audit-backup.db'); s.backup(d); d.close(); s.close()"
	kubectl cp -n $(NAMESPACE) deploy/prahari-bff:/tmp/audit-backup.db \
	  $(BACKUP_DIR)/audit.db
	@echo "backup written to $(BACKUP_DIR) — verify with /api/v1/audit/verify on a restored audit.db"

.PHONY: down
down: ## Uninstall the platform
	helm uninstall prahari --namespace $(NAMESPACE)

.PHONY: loadtest
loadtest: ## Run the load-test harness (infra/loadtest; args via LTARGS, e.g. LTARGS="run --cameras 5,50 --duration-s 60"; default: selftest)
	cd infra/loadtest && ./run.sh $(LTARGS)

.PHONY: nuke
nuke: ## Delete the local cluster entirely
	k3d cluster delete $(CLUSTER)

# --- quality ---------------------------------------------------------------

.PHONY: lint
lint: ## Lint everything that can be linted without a cluster
	uv run ruff check .
	uv run ruff format --check .
	helm lint $(CHART) --values $(CHART)/values-local.yaml
	helm lint $(CHART) --values $(CHART)/values-gpu.yaml
	# Guarded by command -v, not `|| true`: when terraform exists, fmt -check
	# is a real gate and must be allowed to fail the target.
	@if command -v terraform >/dev/null 2>&1; then \
	  terraform -chdir=infra/terraform/envs/demo fmt -check -recursive; \
	else \
	  echo "terraform not installed — skipping fmt check"; \
	fi
	# web lint + typecheck, skipped loudly rather than failing when the
	# toolchain or node_modules is absent on a Python-only checkout.
	@if command -v npm >/dev/null 2>&1 && test -d web/node_modules; then \
	  cd web && npm run lint && npx tsc --noEmit; \
	else \
	  echo "npm or web/node_modules missing — run 'cd web && npm ci' first; skipping web lint"; \
	fi
	cd proto && buf lint

.PHONY: test
test: proto ## Run the test suite across the workspace
	# `pytest` with no path, so packages/ is collected too. A service missing
	# from the run is how a broken service reaches demo day looking green.
	# Depends on `proto`: the stubs are gitignored, so a fresh clone would
	# ImportError on prahari.v1.* before collecting a single test.
	uv run pytest -q

.PHONY: verify
verify: ## Render the chart under both profiles and assert the switch switches
	# Render-only proves the templates parse, not that `profile` changes
	# anything. scripts/verify-profiles.sh asserts the gpu machinery appears
	# under profile=gpu, stays out of profile=local, and that the shared
	# substrate is identical in both renders.
	CHART=$(CHART) ./scripts/verify-profiles.sh
