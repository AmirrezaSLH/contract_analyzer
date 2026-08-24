# Every command in the README, with the paths and flags already right.

PYTHON ?= .venv/bin/python

# The ports live in .env, beside the keys and the paths -- they are
# environment-dependent, not tuning parameters. BACKEND_PORT is the only one a
# demo needs: one process serves the API and the front end it built.
#
# Read one key at a time rather than `-include .env`, which would parse every
# line as a make assignment -- and an API key containing a '#' or a '$' is not
# a make expression.
dotenv = $(shell sed -n 's/^$(1)=//p' .env 2>/dev/null | tail -1)
BACKEND_PORT ?= $(or $(call dotenv,BACKEND_PORT),8100)
FRONTEND_PORT ?= $(or $(call dotenv,FRONTEND_PORT),8101)
export BACKEND_PORT FRONTEND_PORT

.PHONY: help venv ingest reingest search chat analyze api mcp openapi test lint fmt logs \
	ui-install ui-types ui-dev ui-build ui-test \
	docker-build docker-up docker-down docker-logs docker-shell docker-test

help:  ## List the targets
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

venv:  ## Create .venv and install the project with dev extras
	python3 -m venv .venv && $(PYTHON) -m pip install --quiet --upgrade pip && $(PYTHON) -m pip install -e ".[dev]"

ingest:  ## Parse, chunk, embed and store one PDF or a directory: make ingest F=path
	$(PYTHON) scripts/ingest.py "$(F)"

reingest:  ## The same, rebuilding whether or not the file changed
	$(PYTHON) scripts/ingest.py "$(F)" --reingest

search:  ## Vector, keyword and hybrid retrieval side by side: make search Q="..."
	$(PYTHON) scripts/search.py "$(Q)" --mode all

chat:  ## Multi-turn cited conversation over an ingested contract
	$(PYTHON) scripts/chat.py

analyze:  ## The five criteria over one contract: make analyze F=path.pdf
	$(PYTHON) scripts/analyze.py "$(F)"

api:  ## Run the HTTP API, which also serves the built front end (reload on edit)
	$(PYTHON) -m uvicorn contract_analyzer.api.main:app --reload --port $(BACKEND_PORT)

mcp:  ## Run the MCP connector against a locally running API (see MCP-Connector/)
	$(PYTHON) -m mcp_connector $(ARGS)

openapi:  ## Re-export docs/openapi.json, the connector specification
	$(PYTHON) scripts/export_openapi.py

test:  ## The whole suite -- tests/ and MCP-Connector/tests/. No network, no key.
	$(PYTHON) -m pytest -q

lint:  ## ruff
	$(PYTHON) -m ruff check src tests scripts MCP-Connector

fmt:  ## ruff, applying what it can fix
	$(PYTHON) -m ruff check --fix src tests scripts MCP-Connector

logs:  ## Tail the structured JSON log
	tail -f .run/app.jsonl

# --- Front end ---------------------------------------------------------------
# `ui/` is a Vite project at the repository root. It is not a Python package
# and must not look like one. `ui-build` writes the bundle straight into
# src/contract_analyzer/api/static/, which is what StaticFiles serves -- so
# there is no copy step and no volume mount between building and serving.

ui-install:  ## npm ci in ui/
	cd ui && npm ci

ui-types:  ## Re-export the OpenAPI document, then regenerate src/api/types.gen.ts
	$(PYTHON) scripts/export_openapi.py
	cd ui && npm run types

ui-dev:  ## The Vite dev server, proxying /api to a locally running API
	cd ui && npm run dev

ui-build:  ## tsc --noEmit && vite build, into the API package
	cd ui && npm run build

ui-test:  ## vitest: the sse reader, the error map and the depth mapping
	cd ui && npm test

# --- Docker -----------------------------------------------------------------
# The host uid/gid are passed through so bind-mounted data/ and .run/ stay
# host-writable. APP_ prefix: UID is readonly in bash and cannot be assigned.
COMPOSE ?= APP_UID=$(shell id -u) APP_GID=$(shell id -g) docker compose

docker-build:  ## Build the runtime and dev images
	$(COMPOSE) build
	$(COMPOSE) --profile tools build

docker-up:  ## Start the API (and the built UI it serves) in the background
	$(COMPOSE) up -d

docker-down:  ## Stop them. Add V=1 to drop volumes: make docker-down V=1
	$(COMPOSE) down $(if $(V),--volumes,)

docker-logs:  ## Follow the container logs
	$(COMPOSE) logs -f

docker-shell:  ## A shell in the dev image, source bind-mounted
	$(COMPOSE) run --rm tools shell

docker-test:  ## The suite, inside the image
	$(COMPOSE) run --rm tools test
