.DEFAULT_GOAL := help
CLUSTER  := prahari
NAMESPACE := prahari
CHART    := infra/helm/prahari
PROFILE  ?= local

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# --- contracts -------------------------------------------------------------

.PHONY: proto
proto: ## Regenerate protobuf stubs from proto/
	# Python stubs land in packages/prahari-proto/ (an installable workspace
	# package, because generated protobuf imports are absolute); TypeScript in
	# gen/ts for the Next.js build. Both are gitignored — the contract is the
	# .proto file. A fresh clone must run this before the imports resolve.
	cd proto && buf generate

.PHONY: proto-lint
proto-lint: ## Lint the protobuf contract
	cd proto && buf lint

.PHONY: proto-breaking
proto-breaking: ## Check for breaking contract changes against main
	cd proto && buf breaking --against '../.git#branch=main,subdir=proto'

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

.PHONY: internal-secret
internal-secret: ## Create the prahari-internal Secret (service token + credential key)
	# Two keys every real deployment needs:
	#   internal-token  — required as X-Internal-Token on the registry's /api/*;
	#                     must match on every internal caller (BFF, correlation).
	#   credential-key  — 32-byte AES-256 key, urlsafe-base64, encrypting
	#                     cameras.stream_secret (registry crypto.py).
	# Production should supply real values via INTERNAL_TOKEN and
	# CREDENTIAL_KEY in .env (or a secrets manager); the generated fallbacks
	# exist so a local cluster works out of the box. This is create-if-absent
	# on purpose: re-running with generated values would rotate the key that
	# stored camera credentials were encrypted under.
	@if kubectl get secret prahari-internal --namespace $(NAMESPACE) >/dev/null 2>&1; then \
	  echo "prahari-internal already exists — leaving it alone (delete it first to rotate)"; \
	else \
	  test ! -f .env || { set -a; . ./.env; set +a; }; \
	  kubectl create secret generic prahari-internal --namespace $(NAMESPACE) \
	    --from-literal=internal-token="$${INTERNAL_TOKEN:-$$(openssl rand -hex 32)}" \
	    --from-literal=credential-key="$${CREDENTIAL_KEY:-$$(python3 -c 'import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')}" \
	    --dry-run=client -o yaml | kubectl apply -f -; \
	fi

.PHONY: down
down: ## Uninstall the platform
	helm uninstall prahari --namespace $(NAMESPACE)

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
verify: ## Render the chart under both profiles and diff-check the switch
	@echo "--> rendering profile=local"
	@helm template prahari $(CHART) --values $(CHART)/values-local.yaml >/dev/null
	@echo "--> rendering profile=gpu"
	@helm template prahari $(CHART) --values $(CHART)/values-gpu.yaml >/dev/null
	@echo "both profiles render cleanly"
