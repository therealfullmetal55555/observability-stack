.DEFAULT_GOAL := help
SHELL := /bin/bash
COMPOSE := docker compose

PY ?= python3
STACK_NAME ?= observability
GRAFANA_URL ?= http://localhost:3000
PROM_URL ?= http://localhost:9090

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# --- local stack ------------------------------------------------------------

.PHONY: up
up: ## Start the full stack (OTel, Tempo, Prometheus, Loki, Grafana, Alertmanager)
	$(COMPOSE) up -d --wait
	@echo "Grafana      $(GRAFANA_URL)  (admin/admin)"
	@echo "Prometheus   $(PROM_URL)"
	@echo "OTLP gRPC    localhost:4317"

.PHONY: down
down: ## Stop the stack, keep volumes
	$(COMPOSE) down

.PHONY: nuke
nuke: ## Stop the stack and delete all volumes (dashboards survive, data does not)
	$(COMPOSE) down -v

.PHONY: logs
logs: ## Tail logs from every service
	$(COMPOSE) logs -f --tail=100

.PHONY: ps
ps: ## Show container status
	$(COMPOSE) ps

.PHONY: restart
restart: ## Reload collector + prometheus config without dropping volumes
	$(COMPOSE) restart otel-collector prometheus grafana

.PHONY: reload-collector
reload-collector: ## Restart only the collector (use after editing otel-collector/config.yaml)
	$(COMPOSE) restart otel-collector

.PHONY: up-custom
up-custom: ## Start the stack using the custom-built collector (needs Go + docker build)
	$(COMPOSE) --profile custom up -d --build otel-collector-custom

.PHONY: collector-build
collector-build: ## Build the custom collector binary with ocb
	cd otel-collector && go run go.opentelemetry.io/collector/cmd/builder@v0.106.1 --config=builder-config.yaml

.PHONY: test-processor
test-processor: ## Go unit tests for the tokencost processor
	cd otel-collector/processor && go test ./... -race -count=1

.PHONY: lint-processor
lint-processor: ## vet + gofmt check for the custom processor
	cd otel-collector/processor && go vet ./... && test -z "$$(gofmt -l .)"

.PHONY: reload-prometheus
reload-prometheus: ## Hot-reload Prometheus rules via the lifecycle API
	curl -fsS -X POST $(PROM_URL)/-/reload && echo "prometheus reloaded"

# --- validation -------------------------------------------------------------

.PHONY: validate
validate: validate-configs validate-dashboards validate-alerts validate-collector ## Validate every config file

.PHONY: validate-configs
validate-configs: ## YAML syntax + compose schema
	@$(PY) scripts/validate.py --configs

.PHONY: validate-dashboards
validate-dashboards: ## Dashboard JSON structure and datasource refs
	@$(PY) scripts/validate.py --dashboards

.PHONY: validate-alerts
validate-alerts: ## PromQL parse + required labels on every alert rule
	@$(PY) scripts/validate.py --alerts

.PHONY: validate-collector
validate-collector: ## Check every processor/exporter referenced in the pipeline exists
	@$(PY) scripts/validate.py --collector

# --- tests ------------------------------------------------------------------

.PHONY: install
install: ## Install dev dependencies
	$(PY) -m pip install -r requirements.txt
	$(PY) -m pip install -e .

.PHONY: test
test: ## Run the Python test suite
	$(PY) -m pytest tests -q

.PHONY: test-cov
test-cov: ## Tests with coverage report
	$(PY) -m pytest tests -q --cov=instrumentation --cov-report=term-missing

.PHONY: test-n8n
test-n8n: ## Run the n8n node package tests
	cd instrumentation/n8n && npm test

.PHONY: test-processor
test-processor: ## Go unit tests for the tokencost processor
	cd otel-collector/processor && go test ./... -race -count=1

.PHONY: test-all
test-all: validate test test-n8n ## Everything CI can run without Go or a cluster

.PHONY: lint
lint: ## Ruff + black check
	ruff check instrumentation scripts tests
	black --check instrumentation scripts tests

.PHONY: fmt
fmt: ## Auto-format
	ruff check --fix instrumentation scripts tests
	black instrumentation scripts tests

# --- load -------------------------------------------------------------------

.PHONY: load
load: ## Generate synthetic agent traffic (writes to the running collector)
	$(PY) tests/load/generate_traffic.py --duration 60 --rate 20

.PHONY: seed
seed: ## Push a realistic demo dataset so dashboards have something to show
	$(PY) scripts/seed_demo_data.py

# --- packaging / deploy -----------------------------------------------------

.PHONY: package
package: ## Build dist/observability-stack-<version>.tar.gz
	@bash scripts/package.sh

.PHONY: helm-sync
helm-sync: ## Copy dashboards and alert rules into the chart (they are read with .Files.Get)
	cp grafana/dashboards/*.json helm/observability-stack/dashboards/
	cp prometheus/alerts.yml helm/observability-stack/rules/alerts.yml
	@echo "synced — commit the chart copies with the source files"

.PHONY: helm-lint
helm-lint: ## Lint the Helm chart
	helm lint helm/observability-stack

.PHONY: helm-template
helm-template: ## Render the chart with production values
	helm template observability helm/observability-stack -f helm/observability-stack/values-production.yaml

.PHONY: helm-install
helm-install: ## Install into the current kube-context
	helm upgrade --install observability helm/observability-stack \
		-n observability --create-namespace \
		-f helm/observability-stack/values-production.yaml

.PHONY: helm-uninstall
helm-uninstall: ## Remove the release
	helm uninstall observability -n observability

# --- housekeeping -----------------------------------------------------------

.PHONY: dashboards-export
dashboards-export: ## Pull dashboards back out of a running Grafana into grafana/dashboards/
	@$(PY) scripts/export_dashboards.py --url $(GRAFANA_URL)

.PHONY: clean
clean: ## Remove caches and build output
	rm -rf dist .pytest_cache .ruff_cache **/__pycache__ .coverage htmlcov
