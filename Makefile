# =============================================================================
# Nesq Bot - one-word entry points
# =============================================================================
#   make            show this help
#   make up         bring the whole stack up
#   make logs       follow everything
#   make api        run the API on the host with reload
#
# Works on Linux, macOS and Windows (Git Bash / MSYS2).
#
# Windows notes
# -------------
#   * `make` is not installed by default. Get it with one of:
#         winget install GnuWin32.Make      (then add it to PATH)
#         choco install make
#         pacman -S make                    (MSYS2)
#     Every recipe here is also a plain shell command you can copy-paste if you
#     would rather not install make at all.
#   * Recipes run under Git Bash, not cmd.exe, and use forward slashes
#     throughout. MSYS_NO_PATHCONV=1 stops MSYS rewriting arguments that look
#     like Unix paths (it would turn /var/run/docker.sock into C:/...).
#   * The API venv lives at apps/api/.venv; its binaries are in Scripts/ on
#     Windows and bin/ elsewhere. VENV_BIN below picks the right one.
# =============================================================================

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help
.ONESHELL:

COMPOSE ?= docker compose
COMPOSE_DESKTOP := $(COMPOSE) -f docker-compose.yml -f docker-compose.desktop-docker.yml
PYTHON ?= python3

ifeq ($(OS),Windows_NT)
	VENV_BIN := .venv/Scripts
	export MSYS_NO_PATHCONV := 1
	PYTHON := python
else
	VENV_BIN := .venv/bin
endif

API_DIR := apps/api
WORKER_DIR := apps/worker
DESKTOP_DIR := apps/desktop
MOBILE_DIR := apps/mobile

# ANSI only when stdout is a terminal, so piped output stays clean.
ifneq (,$(findstring xterm,$(TERM)))
	BOLD := \033[1m
	DIM := \033[2m
	RESET := \033[0m
else
	BOLD :=
	DIM :=
	RESET :=
endif

.PHONY: help
help: ## Show this help
	@printf "$(BOLD)Nesq Bot$(RESET)\n\n"
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@printf "\n$(DIM)First run: make env && make up$(RESET)\n"

# -----------------------------------------------------------------------------
# Stack
# -----------------------------------------------------------------------------

.PHONY: env
env: ## Create .env from .env.example if it does not exist
	@if [ -f .env ]; then
		echo ".env already exists - leaving it alone"
	else
		cp .env.example .env
		echo "created .env from .env.example"
	fi

.PHONY: up
up: env ## Start the stack (mock desktops, no Docker socket)
	$(COMPOSE) up -d
	@echo
	@echo "API          http://localhost:8080/api/health"
	@echo "Temporal UI  http://localhost:8088"
	@echo
	@echo "Watch it settle: make logs"

.PHONY: up-desktop
up-desktop: env ## Start the stack with real Docker-spawned bot desktops
	@echo "Mounting the host Docker socket into the API - root-equivalent access."
	$(COMPOSE_DESKTOP) up -d

.PHONY: down
down: ## Stop the stack (volumes survive)
	$(COMPOSE) down --remove-orphans

.PHONY: nuke
nuke: ## Stop the stack and DELETE the postgres/redis volumes
	$(COMPOSE) down --remove-orphans --volumes
	@echo "volumes removed - the next 'make up' re-runs init.sql"

.PHONY: logs
logs: ## Follow logs for everything
	$(COMPOSE) logs -f --tail=100

.PHONY: ps
ps: ## Show service status and health
	$(COMPOSE) ps

.PHONY: restart
restart: ## Recreate api and worker (after a code change)
	$(COMPOSE) up -d --build --force-recreate api worker

.PHONY: build
build: ## Build the api and worker images
	$(COMPOSE) build api worker

# -----------------------------------------------------------------------------
# Run components on the host
# -----------------------------------------------------------------------------

.PHONY: venv
venv: ## Create the API/worker virtualenvs and install dependencies
	@for dir in $(API_DIR) $(WORKER_DIR); do
		if [ ! -d "$$dir/.venv" ]; then
			echo "creating $$dir/.venv"
			(cd "$$dir" && $(PYTHON) -m venv .venv)
		fi
		echo "installing $$dir dependencies"
		if [ -f "$$dir/requirements-dev.txt" ]; then
			"$$dir/$(VENV_BIN)/python" -m pip install -q --upgrade pip
			"$$dir/$(VENV_BIN)/pip" install -q -r "$$dir/requirements-dev.txt"
		else
			"$$dir/$(VENV_BIN)/python" -m pip install -q --upgrade pip
			"$$dir/$(VENV_BIN)/pip" install -q -r "$$dir/requirements.txt"
		fi
	done
	@echo "done"

.PHONY: api
api: ## Run the API on the host with reload (needs: make up)
	@cd $(API_DIR) && \
		BOTS_DIR=../../bots \
		BOT_DESKTOP_MODE=$${BOT_DESKTOP_MODE:-mock} \
		$(VENV_BIN)/uvicorn app.main:app --reload --host 0.0.0.0 --port 8080

.PHONY: worker
worker: ## Run the Temporal worker on the host
	@cd $(WORKER_DIR) && $(VENV_BIN)/python -m worker.main

.PHONY: desktop
desktop: ## Run the Tauri/Vite desktop app (http://localhost:1420)
	npm run dev --prefix $(DESKTOP_DIR)

.PHONY: mobile
mobile: ## Run the Expo mobile app
	npm run start --prefix $(MOBILE_DIR)

.PHONY: install
install: ## npm install for every workspace
	npm install --no-audit --no-fund
	@for dir in packages/protocol packages/ui packages/connector-sdk packages/model-router $(DESKTOP_DIR) $(MOBILE_DIR); do
		if [ -f "$$dir/package.json" ]; then
			echo "installing $$dir"
			npm install --prefix "$$dir" --no-audit --no-fund
		fi
	done

# -----------------------------------------------------------------------------
# Bot Desktop image
# -----------------------------------------------------------------------------

.PHONY: desktop-image
desktop-image: ## Build nesqbot/bot-desktop:local (slim / IceWM)
	docker build \
		--build-arg DESKTOP_PROFILE_BUILD=$${DESKTOP_PROFILE_BUILD:-slim} \
		-t nesqbot/bot-desktop:local \
		infra/bot-desktop

.PHONY: desktop-image-full
desktop-image-full: ## Build the XFCE variant of the bot desktop image
	docker build \
		--build-arg DESKTOP_PROFILE_BUILD=full \
		-t nesqbot/bot-desktop:xfce \
		infra/bot-desktop

.PHONY: desktop-shell
desktop-shell: ## Shell into a throwaway bot desktop container
	docker run --rm -it --entrypoint bash \
		-p 6901:6901 -p 7910:7910 --shm-size 512m \
		nesqbot/bot-desktop:local

# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------

.PHONY: psql
psql: ## Open psql against the running postgres container
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-nesq} -d $${POSTGRES_DB:-nesqbot}

.PHONY: reset-db
reset-db: ## Drop the postgres volume and re-run init.sql (DESTRUCTIVE)
	@printf "This deletes every row in the local database. Continue? [y/N] "
	@read -r reply; [ "$$reply" = "y" ] || { echo "aborted"; exit 1; }
	$(COMPOSE) rm -sf postgres
	docker volume rm -f nesqbot_pg_data
	$(COMPOSE) up -d postgres
	@echo "waiting for postgres to initialise..."
	@for _ in $$(seq 1 60); do
		if $(COMPOSE) exec -T postgres pg_isready -U $${POSTGRES_USER:-nesq} -d $${POSTGRES_DB:-nesqbot} >/dev/null 2>&1; then
			echo "postgres is ready - pgvector and the schema are in place"
			exit 0
		fi
		sleep 1
	done
	@echo "postgres did not become ready in 60s - check: make logs" >&2
	@exit 1

.PHONY: seed
seed: ## Re-seed system bots, connectors and the KB from bots/*.yaml
	@# The API runs seed_system() in its lifespan hook and the loader is
	@# idempotent (existing slugs are left alone), so restarting the service is
	@# the seed - and it cannot drift from however the app wires its session.
	@if ! $(COMPOSE) ps --status running --services 2>/dev/null | grep -qx api; then
		echo "the api service is not running - start it with: make up" >&2
		exit 1
	fi
	@echo "restarting api to re-run the seed loader against bots/*.yaml"
	$(COMPOSE) restart api
	@for _ in $$(seq 1 60); do
		if curl -fsS "http://localhost:$${API_HOST_PORT:-8080}/api/health" >/dev/null 2>&1; then
			echo "seeded - $$(curl -fsS "http://localhost:$${API_HOST_PORT:-8080}/api/bots" -H 'X-Nesq-Dev: 1' | grep -o '"slug"' | wc -l) bots present"
			exit 0
		fi
		sleep 1
	done
	@echo "the api did not come back within 60s - check: make logs" >&2
	@exit 1

# -----------------------------------------------------------------------------
# Quality
# -----------------------------------------------------------------------------

.PHONY: test
test: ## Run the Python test suites
	@status=0
	@for dir in $(API_DIR) $(WORKER_DIR); do
		if [ -d "$$dir/tests" ]; then
			echo "== pytest $$dir"
			(cd "$$dir" && $(VENV_BIN)/python -m pytest -q) || status=1
		else
			echo "== $$dir has no tests/ yet - skipping"
		fi
	done
	exit $$status

.PHONY: lint
lint: ## Ruff + shellcheck + compose validation
	@status=0
	@for dir in $(API_DIR) $(WORKER_DIR); do
		echo "== ruff $$dir"
		(cd "$$dir" && $(VENV_BIN)/python -m ruff check .) || status=1
	done
	@echo "== bash -n"
	@for script in $$(find . -name '*.sh' -not -path './node_modules/*' -not -path '*/node_modules/*'); do
		bash -n "$$script" || status=1
	done
	@echo "== docker compose config"
	@$(COMPOSE) config >/dev/null || status=1
	exit $$status

.PHONY: fmt
fmt: ## Format Python with ruff
	@for dir in $(API_DIR) $(WORKER_DIR); do
		echo "== ruff format $$dir"
		(cd "$$dir" && $(VENV_BIN)/python -m ruff format . && $(VENV_BIN)/python -m ruff check --fix .)
	done

.PHONY: typecheck
typecheck: ## Typecheck the TypeScript packages
	@for dir in packages/protocol packages/ui packages/connector-sdk packages/model-router $(DESKTOP_DIR); do
		if [ -f "$$dir/package.json" ]; then
			echo "== typecheck $$dir"
			npm run typecheck --prefix "$$dir"
		fi
	done

# -----------------------------------------------------------------------------
# Infra
# -----------------------------------------------------------------------------

.PHONY: bicep
bicep: ## Build the Bicep templates and both parameter files (no deployment)
	az bicep build --file infra/azure/main.bicep --outfile /dev/null
	az bicep build-params --file infra/azure/main.bicepparam --outfile /dev/null
	az bicep build-params --file infra/azure/prod.bicepparam --outfile /dev/null
	@echo "main.bicep and both parameter files build cleanly"

.PHONY: whatif
whatif: ## az deployment what-if (dev params) against $$AZURE_RESOURCE_GROUP
	@: $${AZURE_RESOURCE_GROUP:?set AZURE_RESOURCE_GROUP first}
	@: $${NESQBOT_PG_PASSWORD:?set the NESQBOT_* secrets - see infra/azure/README.md}
	az deployment group what-if \
		--resource-group "$$AZURE_RESOURCE_GROUP" \
		--template-file infra/azure/main.bicep \
		--parameters infra/azure/main.bicepparam

.PHONY: whatif-prod
whatif-prod: ## az deployment what-if (prod params) against $$AZURE_RESOURCE_GROUP
	@: $${AZURE_RESOURCE_GROUP:?set AZURE_RESOURCE_GROUP first}
	@: $${NESQBOT_PG_PASSWORD:?set the NESQBOT_* secrets - see infra/azure/README.md}
	az deployment group what-if \
		--resource-group "$$AZURE_RESOURCE_GROUP" \
		--template-file infra/azure/main.bicep \
		--parameters infra/azure/prod.bicepparam

.PHONY: azure-preflight
azure-preflight: ## One-time subscription prerequisites for the Azure stack
	az provider register -n Microsoft.ContainerInstance --wait
	az provider register -n Microsoft.App --wait
	az provider register -n Microsoft.OperationalInsights --wait
	@echo "providers registered"

.PHONY: health
health: ## Curl every local health endpoint
	@echo "== api"
	@curl -fsS http://localhost:8080/api/health || echo "  api is not answering"
	@echo
	@echo "== api deep"
	@curl -fsS http://localhost:8080/api/health/deep || echo "  deep check failed"
	@echo
	@echo "== bot desktop sidecar"
	@curl -fsS http://localhost:7910/health || echo "  no desktop running (expected in mock mode)"
	@echo
