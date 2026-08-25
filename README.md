# contract_analyzer

Compliance analysis of PDF contracts: parse, chunk, embed, retrieve, and
assess against five requirements with verbatim quotes and section references.
A React front end and an HTTP API sit on top; `make analyze` runs the same
pipeline from the command line.

Each verdict comes from three agents rather than one model call — an
**Analyzer** that searches and drafts, an **Evaluator** shown only the quotes
and the claims, and a **Router** that runs both and decides whether the answer
is ready. Start at [docs/agents/](docs/agents/README.md).

## Docs

| | |
|---|---|
| [architecture.md](docs/architecture.md) | the master document: layers, modules, status |
| [agents/](docs/agents/README.md) | the three agents, the protocol between them, and the failure strategy |
| [agents/confidence.md](docs/agents/confidence.md) | what the confidence number means, and why it is not called calibrated |
| [generation.md](docs/generation.md) | the agent loop, the tools, the two finishers |
| [compliance.md](docs/compliance.md) | the criteria, the result schema, the structural validator |
| [retrieval.md](docs/retrieval.md) · [chunking.md](docs/chunking.md) · [parsing.md](docs/parsing.md) · [ingestion.md](docs/ingestion.md) | PDF to searchable chunks |
| [api.md](docs/api.md) · [ui.md](docs/ui.md) | the HTTP surface and the front end |
| [storage.md](docs/storage.md) · [logging.md](docs/logging.md) · [http-client.md](docs/http-client.md) · [configuration.md](docs/configuration.md) | SQLite, structured logs, one retrying transport, settings |
| [docker.md](docs/docker.md) | build and run the whole thing in a container |

Design documents and implementation reports live in `plan_implement_docs/`.

## Getting started

refer to `GET_STARTED.md

`ANTHROPIC_API_KEY` in `.env` at the project root is what generation needs;
retrieval (`make search`) works without one.
