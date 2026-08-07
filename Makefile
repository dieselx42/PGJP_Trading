# =============================================================================
# sol-futures-trading-bot
#
# There is deliberately no target that enables trading, clears the kill switch,
# or switches to live mode. Those are configuration changes a human makes to the
# server .env, followed by a restart.
# =============================================================================

SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTHON      ?= python3.12
VENV        ?= .venv
BIN         := $(VENV)/bin
COMPOSE     ?= docker compose
SERVICE     := sol-trading-bot
GIT_COMMIT  := $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
BUILD_TS    := $(shell date -u +%Y-%m-%dT%H:%M:%SZ)

export GIT_COMMIT
export BUILD_TIMESTAMP = $(BUILD_TS)

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# -----------------------------------------------------------------------------
# Local development
# -----------------------------------------------------------------------------

.PHONY: install
install: ## Create the virtualenv and install dev dependencies
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e '.[dev]'
	@echo "Done. Activate with: source $(BIN)/activate"

.PHONY: test
test: ## Run the full test suite
	$(BIN)/python -m pytest

.PHONY: test-safety
test-safety: ## Run only the critical trading-safety tests
	$(BIN)/python -m pytest -m safety -v

.PHONY: coverage
coverage: ## Run tests with a coverage report
	$(BIN)/python -m pytest --cov=app --cov-report=term-missing

.PHONY: lint
lint: ## Run ruff (lint + format check)
	$(BIN)/ruff check app tests scripts
	$(BIN)/ruff format --check app tests

.PHONY: format
format: ## Apply ruff formatting and safe fixes
	$(BIN)/ruff format app tests
	$(BIN)/ruff check --fix app tests

.PHONY: typecheck
typecheck: ## Run mypy in strict mode
	$(BIN)/mypy app

.PHONY: check
check: lint typecheck test ## Everything CI runs, locally

.PHONY: run-local
run-local: ## Run the bot outside Docker using .env.local
	set -a; [ -f .env.local ] && . ./.env.local; set +a; $(BIN)/python -m app.main

# -----------------------------------------------------------------------------
# Docker
# -----------------------------------------------------------------------------

.PHONY: dirs
dirs: ## Create ./data and ./logs with the container's uid/gid
	@bash scripts/bootstrap_dirs.sh

.PHONY: build
build: ## Build the container image
	$(COMPOSE) build

.PHONY: run
run: dirs ## Start the bot (detached)
	@bash scripts/verify_safety.sh .env
	$(COMPOSE) up -d
	@echo "Started. Check with: make status"

.PHONY: stop
stop: ## Stop the bot
	$(COMPOSE) down

.PHONY: restart
restart: ## Restart the bot
	$(COMPOSE) restart $(SERVICE)

.PHONY: logs
logs: ## Follow container logs
	$(COMPOSE) logs -f --tail=200 $(SERVICE)

.PHONY: ps
ps: ## Show container state and health
	$(COMPOSE) ps

.PHONY: shell
shell: ## Open a shell inside the running container
	$(COMPOSE) exec $(SERVICE) /bin/bash

# -----------------------------------------------------------------------------
# Operator commands (all read-only except kill-switch-on)
# -----------------------------------------------------------------------------

.PHONY: status
status: ## Full application status
	@$(COMPOSE) exec -T $(SERVICE) python -m app.cli status

.PHONY: health
health: ## Health endpoint only
	@$(COMPOSE) exec -T $(SERVICE) python /app/scripts/healthcheck.py && echo "healthy"

.PHONY: broker-status
broker-status: ## Broker configuration and recent events
	@$(COMPOSE) exec -T $(SERVICE) python -m app.cli broker-status

.PHONY: positions
positions: ## Current positions
	@$(COMPOSE) exec -T $(SERVICE) python -m app.cli positions

.PHONY: open-orders
open-orders: ## Working orders
	@$(COMPOSE) exec -T $(SERVICE) python -m app.cli open-orders

.PHONY: contract-info
contract-info: ## Qualified contract metadata
	@$(COMPOSE) exec -T $(SERVICE) python -m app.cli contract-info

.PHONY: db-info
db-info: ## Database file, schema version, and row counts
	@$(COMPOSE) exec -T $(SERVICE) python -m app.cli db-info

.PHONY: kill-switch-status
kill-switch-status: ## Report the kill switch (config + durable latch)
	@$(COMPOSE) exec -T $(SERVICE) python -m app.cli kill-switch-status

.PHONY: kill-switch-on
kill-switch-on: ## Engage the kill switch durably (REASON="...")
	@test -n "$(REASON)" || (echo 'Usage: make kill-switch-on REASON="why"'; exit 1)
	@$(COMPOSE) exec -T $(SERVICE) python -m app.cli kill-switch-on --reason "$(REASON)"

.PHONY: cancel-all-orders
cancel-all-orders: ## Cancel every working order (does NOT close positions)
	@$(COMPOSE) exec -T $(SERVICE) python -m app.cli cancel-all-orders --confirm \
	  --reason "$(or $(REASON),operator request)"

# -----------------------------------------------------------------------------
# Safety verification
# -----------------------------------------------------------------------------

.PHONY: verify-safety
verify-safety: ## Assert the server .env is not configured for live trading
	@bash scripts/verify_safety.sh .env

.PHONY: audit-vps
audit-vps: ## Print the VPS audit script (run it ON the server)
	@echo "Run this on srv1792440.hstgr.cloud:"
	@echo "  bash scripts/vps_audit.sh > vps-audit.txt 2>&1"

.PHONY: clean
clean: ## Remove caches and build artefacts (never touches data/ or logs/)
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info htmlcov .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
