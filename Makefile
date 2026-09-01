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

.PHONY: down
down: ## Uninstall the platform
	helm uninstall prahari --namespace $(NAMESPACE)

.PHONY: nuke
nuke: ## Delete the local cluster entirely
	k3d cluster delete $(CLUSTER)

# --- quality ---------------------------------------------------------------

.PHONY: lint
lint: ## Lint everything that can be linted without a cluster
	helm lint $(CHART) --values $(CHART)/values-local.yaml
	terraform -chdir=infra/terraform/envs/dev fmt -check -recursive || true
	cd proto && buf lint

.PHONY: test
test: ## Run the test suite across services
	uv run pytest services -q

.PHONY: verify
verify: ## Render the chart under both profiles and diff-check the switch
	@echo "--> rendering profile=local"
	@helm template prahari $(CHART) --values $(CHART)/values-local.yaml >/dev/null
	@echo "--> rendering profile=gpu"
	@helm template prahari $(CHART) --values $(CHART)/values-gpu.yaml >/dev/null
	@echo "both profiles render cleanly"
