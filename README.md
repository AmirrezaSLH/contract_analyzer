# contract_analyzer

Upload a PDF contract and get a verdict on each of five security requirements,
with a verbatim quote and a section reference behind every claim.

## Quick start

Needs **Python ≥ 3.11** and **Node ≥ 18** on PATH. Nothing else.

```bash
./setup.bash            # venv, npm ci, UI bundle, and .env  (~2 min)
#                         now paste your keys into .env -- see below
./start.bash            # API and UI on http://localhost:8100
```

Open <http://localhost:8100>, upload a PDF, and run the analysis.

Ctrl-C stops everything. `./stop.bash` is the safety net if a process is left
behind. `./setup.bash --clean` rebuilds `.venv` and `ui/node_modules` from
scratch.

### Keys

Both go in `.env`:

| | |
|---|---|
| `ANTHROPIC_API_KEY` | chat and compliance analysis. Without it, upload and the library still work. |
| `OPENAI_API_KEY` | embeddings, used at ingest time. For a keyless demo, set `"embedding_provider": "fake"` in `settings.json`. |

### Other ways to run it

```bash
./start.bash --dev      # Vite dev server on :8101, proxying /api
make docker-up          # same app in a container, same port  (make docker-build first)
make analyze F=path.pdf # the five criteria from the command line
make test               # the suite: no network, no key
make help               # every other target
```

Ports live in `.env` (`BACKEND_PORT`, `FRONTEND_PORT`, `MCP_PORT`); everything
else is tuning and lives in `settings.json`. See
[docs/configuration.md](docs/configuration.md).

## Layout

```
src/contract_analyzer/   the Python package
├── parse/               PDF to text blocks, tables, figures, outline
├── ingest/              chunking and the ingest pipeline
├── embeddings/          openai · local · fake providers
├── retrieval/           vector, keyword and hybrid search over chunks
├── generation/          the agents: analyzer, evaluator, router, tools, prompts
├── compliance/          the five criteria, the result schema, the validator
├── metrics/             KPI store, samplers, monitor queries
└── api/                 FastAPI app and routes; api/static/ is the built UI

ui/                      React + Vite front end (src/, tests in test/)
MCP-Connector/           MCP server in front of the HTTP API
scripts/                 analyze.py (CLI analysis), export_openapi.py
tests/                   the pytest suite
docs/                    the documentation linked below
docker/                  entrypoint for the image; see Dockerfile, docker-compose.yml
plan_implement_docs/     design notes and implementation reports
presentation/            the demo deck
data/ · .run/            written at setup: PDFs, contracts.db, app.jsonl

Makefile                 every command with the flags already right (make help)
settings.json            tuning: models, chunking, retrieval, effort levels
.env.example             secrets, paths and ports -- copy to .env
setup.bash · start.bash · stop.bash    install · run · clean up
```

## Docs

| | |
|---|---|
| [architecture.md](docs/architecture.md) | the master document: layers, modules, status |
| [agents/](docs/agents/README.md) | the three agents, the protocol between them, and the failure strategy |
| [agents/confidence.md](docs/agents/confidence.md) | what the confidence number means, and why it is not called calibrated |
| [generation.md](docs/generation.md) | the agent loop, the tools, the two finishers |
| [compliance.md](docs/compliance.md) | the criteria, the result schema, the structural validator |
| [retrieval.md](docs/retrieval.md) · [chunking.md](docs/chunking.md) · [parsing.md](docs/parsing.md) · [ingestion.md](docs/ingestion.md) | PDF to searchable chunks |
| [api.md](docs/api.md) · [ui.md](docs/ui.md) · [mcp.md](docs/mcp.md) | the HTTP surface, the front end, and the MCP connector |
| [storage.md](docs/storage.md) · [logging.md](docs/logging.md) · [http-client.md](docs/http-client.md) · [configuration.md](docs/configuration.md) | SQLite, structured logs, one retrying transport, settings |
| [metrics.md](docs/metrics.md) | how a number on the KPI page becomes a query |
| [docker.md](docs/docker.md) | build and run the whole thing in a container |
