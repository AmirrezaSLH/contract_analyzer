# Configuration

`src/contract_analyzer/config.py` -- a `pydantic-settings` model assembled
from two files at the project root, once, via `get_settings()` (cached):

- **`.env`** -- secrets and paths that differ per environment (a local
  checkout vs. the Docker container vs. CI). `.env.example` documents every
  field; copy it to `.env`.
- **`settings.json`** -- tuning parameters: model choice, effort levels, tool
  caps, chunking, retrieval, HTTP, logging verbosity, embeddings. The same
  value everywhere a given checkout runs, so it's versioned with the code
  instead of read from the environment. Optional -- a missing key, or a
  missing file, falls back to the field default in `config.py`.

Precedence, highest first: process env vars > `.env` > `settings.json` >
field defaults. In practice that means `.env` only ever needs the fields
below, and `settings.json` only ever needs the fields in the second table.

## `.env` fields

| Field (env var) | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | `SecretStr`; never printed |
| `OPENAI_API_KEY` | – | `SecretStr` |
| `DB_PATH` | `data/contracts.db` | |
| `RAW_DIR` | `data/raw` | uploaded/ingested PDFs |
| `ASSETS_DIR` | `data/assets` | extracted figure images |
| `LOG_FILE` | `.run/app.jsonl` | blank disables the JSON file |

## `settings.json` fields

| Group | Field | Default | Notes |
|---|---|---|---|
| Generation | `answer_model` | `claude-opus-5` | chat/citation model |
| | `answer_max_tokens` | 8000 | streaming, so generous |
| | `chat_effort` | `low` | `output_config.effort` for the chat loop and finisher |
| | `analysis_effort` | `medium` | the same for the compliance analysis; where to spend |
| | `chat_max_tool_calls` | 4 | tool executions per chat run |
| | `analysis_max_tool_calls` | 8 | per criterion |
| | `max_evidence_tokens` | 12000 | the evidence ledger's total per run |
| | `structure_fix_rounds` | 2 | structural self-correction rounds before `needs_review` |
| | `prompts_path` | `src/…/generation/prompts.json` | the prompt library, validated on load |
| HTTP | `http_timeout_seconds` | 60 | see http-client.md |
| | `http_retries` | 3 | retries after the first attempt |
| Logging | `log_level` | `INFO` | |
| Embeddings | `embedding_provider` | `openai` | `openai` / `local` / `fake` |
| | `embedding_model` | provider default | `text-embedding-3-small`, `BAAI/bge-small-en-v1.5`, `fake-hash` |
| | `embedding_dim` | 512 | fixed at DB creation; `local` must be 384 |
| Chunking | `chunk_tokens` | 400 | one or two sub-clauses |
| | `chunk_overlap_tokens` | 80 | whole-element overlap |
| Retrieval | `retrieval_mode` | `hybrid` | default, overridable per call |
| | `retrieval_candidates` | 20 | per retriever, before fusion |
| | `retrieval_top_k` | 6 | |
| | `rrf_k` | 60 | |

## Four behaviours worth knowing

* **Why the split.** `.env` is gitignored and read once per environment;
  `settings.json` is committed and read once per checkout. A key or a bind
  mount changes between machines, so it belongs in `.env`. A model name or a
  chunk size should be the same on every machine running this code, so it
  belongs in `settings.json` and travels with a commit like any other file.
* **Blank means default.** `EMBEDDING_MODEL: null` in `settings.json` and
  `LOG_FILE=` in `.env` are treated as unset, so the example files can list
  every key without forcing a value.
* **Relative paths anchor to the project root**, not the working directory,
  so `make ingest`, pytest and a script run from anywhere agree on where the
  database is. This applies to `.env` paths and to `prompts_path` in
  `settings.json` alike.
* **`validate_embedding_dim()`** refuses a width the active provider cannot
  emit before anything is written -- a `vec0` table's width is fixed at
  creation and a mismatch discovered mid-ingest would mean a rebuild.

Secrets are `SecretStr`: a logged or printed `Settings` shows `**********`.
The clear value is read at the one point of use (`settings.anthropic_key`,
`settings.openai_key`).
