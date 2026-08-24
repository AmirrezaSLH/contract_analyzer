# Phase A — implementation report (in progress)

Companion to `01_foundation_plan.md`. Updated at each checkpoint; the final
version is written when the phase closes.

## Checkpoint 1 — 2026-08-23

### Commits

| Commit | Plan step | Notes |
|---|---|---|
| `da87f42 docs: implementation plan…` | – | plan files + `AGENTS.md` |
| `c93800c chore: project scaffold` | 1 | `pyproject.toml`, `Makefile`, `.env.example`, `.gitignore`; criteria JSON moved into the package |
| `f19a71c feat(core): logger…` | 2 | `logger.py` + 6 tests |
| `b3bb72a feat(core): retrying HTTP client…` | 3 | `http_client.py` + 13 tests |
| `8d62097 style: wrap two long lines` | – | ruff E501 |
| `e3b6eb4 feat(core): settings, SQLite store…` | 4 | `config.py`, `db.py`, `schema.sql`, `tokens.py` |
| `5a0b763 feat(parse): PyMuPDF parser…` | 5 | `parse/` + `models.py`; `describe.py` on logger + shared HTTP client |

### Verification

* `pytest`: 20 passed, offline. `ruff`: clean.
* Copied parser on `Sample Contract.pdf`: 21 pages, 51 headings, 52
  paragraphs, 42 ruled tables, 0 furniture, **0 sections** (no outline) --
  the gap step 6 closes.

### Deviations from the plan

* **`httpx` → `httpx2`.** anthropic 1.0 and openai 3.3 require an
  `httpx2.Client`. Same API; `http_client.py` aliases it and `pyproject`
  depends on `httpx2`.
* `models.py` moved from step 4 to step 5 because it imports `parse.elements`.
* Environment: `pip` and `git commit` each segfaulted once (likely the
  `/media` mount); no data lost, retried successfully. The project `.venv`
  install is slow on this mount; tests were run with RAG_Mock's venv
  (`PYTHONPATH=src`).

### Remaining

Steps 6–14: section spine synthesis, chunker/pipeline, embeddings, retrieval
with `document_id`, cited chat, CLIs, test port, sample parse report, docs.
