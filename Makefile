# Every command in the README, with the paths and flags already right.

PYTHON ?= .venv/bin/python

.PHONY: help venv ingest reingest search chat test lint fmt logs

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

test:  ## The whole suite. No network, no key, no corpus files.
	$(PYTHON) -m pytest -q

lint:  ## ruff
	$(PYTHON) -m ruff check src tests scripts

fmt:  ## ruff, applying what it can fix
	$(PYTHON) -m ruff check --fix src tests scripts

logs:  ## Tail the structured JSON log
	tail -f .run/app.jsonl
