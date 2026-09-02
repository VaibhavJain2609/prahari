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
images: ## Build the service images into the k3d registry
	# Built from the workspace root: every service depends on
	# packages/prahari-common, which a narrower build context would exclude.
	docker build -f services/registry/Dockerfile     -t localhost:5555/prahari-registry:dev     .
	docker build -f services/inference/Dockerfile    -t localhost:5555/prahari-inference:dev    .
	docker build -f services/match-engine/Dockerfile -t localhost:5555/prahari-match-engine:dev .
	docker push localhost:5555/prahari-registry:dev
	docker push localhost:5555/prahari-inference:dev
	docker push localhost:5555/prahari-match-engine:dev

.PHONY: gateway-secret
gateway-secret: ## Load .env into the cluster as the gateway credential Secret
	# The credential never enters values.yaml, tfvars or a commit. This reads
	# the gitignored .env and nothing else.
	@test -f .env || { echo "no .env — copy .env.example and fill it in"; exit 1; }
	kubectl create secret generic prahari-gateway \
	  --namespace $(NAMESPACE) --from-env-file=.env \
	  --dry-run=client -o yaml | kubectl apply -f -

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
	terraform -chdir=infra/terraform/envs/dev fmt -check -recursive || true
	cd proto && buf lint

.PHONY: test
test: ## Run the test suite across the workspace
	# `pytest` with no path, so packages/ is collected too. A service missing
	# from the run is how a broken service reaches demo day looking green.
	uv run pytest -q

.PHONY: verify
verify: ## Render the chart under both profiles and diff-check the switch
	@echo "--> rendering profile=local"
	@helm template prahari $(CHART) --values $(CHART)/values-local.yaml >/dev/null
	@echo "--> rendering profile=gpu"
	@helm template prahari $(CHART) --values $(CHART)/values-gpu.yaml >/dev/null
	@echo "both profiles render cleanly"
