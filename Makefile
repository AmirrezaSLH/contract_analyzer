# Every command in the README, with the paths and flags already right.

PYTHON ?= .venv/bin/python

.PHONY: help venv ingest reingest search chat analyze api ui openapi test lint fmt logs \
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

api:  ## Run the HTTP API locally on :8000 (reload on edit)
	$(PYTHON) -m uvicorn contract_analyzer.api.main:app --reload --port $(or $(PORT),8000)

ui:  ## Run the Streamlit front end on :8501 against a local API
	CA_API_URL=$(or $(API_URL),http://localhost:8000) \
	$(PYTHON) -m streamlit run src/contract_analyzer/ui/app.py --server.port $(or $(UI_PORT),8501)

openapi:  ## Re-export docs/openapi.json, the connector specification
	$(PYTHON) scripts/export_openapi.py

test:  ## The whole suite. No network, no key, no corpus files.
	$(PYTHON) -m pytest -q

lint:  ## ruff
	$(PYTHON) -m ruff check src tests scripts

fmt:  ## ruff, applying what it can fix
	$(PYTHON) -m ruff check --fix src tests scripts

logs:  ## Tail the structured JSON log
	tail -f .run/app.jsonl

# --- Docker -----------------------------------------------------------------
# The host uid/gid are passed through so bind-mounted data/ and .run/ stay
# host-writable. APP_ prefix: UID is readonly in bash and cannot be assigned.
COMPOSE ?= APP_UID=$(shell id -u) APP_GID=$(shell id -g) docker compose

docker-build:  ## Build the runtime and dev images
	$(COMPOSE) build
	$(COMPOSE) --profile tools build

docker-up:  ## Start the API and UI in the background
	$(COMPOSE) up -d

docker-down:  ## Stop them. Add V=1 to drop volumes: make docker-down V=1
	$(COMPOSE) down $(if $(V),--volumes,)

docker-logs:  ## Follow the container logs
	$(COMPOSE) logs -f

docker-shell:  ## A shell in the dev image, source bind-mounted
	$(COMPOSE) run --rm tools shell

docker-test:  ## The suite, inside the image
	$(COMPOSE) run --rm tools test
