# Configuration

`src/contract_analyzer/config.py` -- a `pydantic-settings` model read from the
process environment and from `.env` at the project root, once, via
`get_settings()` (cached). `.env.example` documents every field; copy it to
`.env`.

## Fields

| Group | Field (env var) | Default | Notes |
|---|---|---|---|
| Generation | `ANTHROPIC_API_KEY` | – | `SecretStr`; never printed |
| | `ANSWER_MODEL` | `claude-opus-5` | chat/citation model |
| | `ANSWER_MAX_TOKENS` | 8000 | streaming, so generous |
| | `ANSWER_EFFORT` | `low` | `output_config.effort`; the cost lever |
| | `PROMPTS_PATH` | `src/…/generation/prompts.json` | the chat prompt library, validated on load |
| HTTP | `HTTP_TIMEOUT_SECONDS` | 60 | see http-client.md |
| | `HTTP_RETRIES` | 3 | retries after the first attempt |
| Logging | `LOG_LEVEL` | `INFO` | |
| | `LOG_FILE` | `.run/app.jsonl` | blank disables the JSON file |
| Embeddings | `EMBEDDING_PROVIDER` | `openai` | `openai` / `local` / `fake` |
| | `EMBEDDING_MODEL` | provider default | `text-embedding-3-small`, `BAAI/bge-small-en-v1.5`, `fake-hash` |
| | `EMBEDDING_DIM` | 512 | fixed at DB creation; `local` must be 384 |
| | `OPENAI_API_KEY` | – | `SecretStr` |
| Storage | `DB_PATH` | `data/contracts.db` | |
| | `RAW_DIR` | `data/raw` | uploaded/ingested PDFs |
| | `ASSETS_DIR` | `data/assets` | extracted figure images |
| Chunking | `CHUNK_TOKENS` | 400 | one or two sub-clauses |
| | `CHUNK_OVERLAP_TOKENS` | 80 | whole-element overlap |
| Retrieval | `RETRIEVAL_MODE` | `hybrid` | default, overridable per call |
| | `RETRIEVAL_CANDIDATES` | 20 | per retriever, before fusion |
| | `RETRIEVAL_TOP_K` | 6 | |
| | `RRF_K` | 60 | |

## Three behaviours worth knowing

* **Blank means default.** `EMBEDDING_MODEL=` and `LOG_FILE=` in `.env` are
  treated as unset, so the example file can list every key without forcing a
  value.
* **Relative paths anchor to the project root**, not the working directory,
  so `make ingest`, pytest and a script run from anywhere agree on where the
  database is.
* **`validate_embedding_dim()`** refuses a width the active provider cannot
  emit before anything is written -- a `vec0` table's width is fixed at
  creation and a mismatch discovered mid-ingest would mean a rebuild.

Secrets are `SecretStr`: a logged or printed `Settings` shows `**********`.
The clear value is read at the one point of use (`settings.anthropic_key`,
`settings.openai_key`).
