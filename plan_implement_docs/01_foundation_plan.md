# Phase A — Foundation: parse → chunk → embed → retrieve → cited conversation

Scope agreed 2026-08-23. **Out of scope for this phase:** FastAPI, Streamlit, MCP,
KPI dashboard, metrics store, agentic analyzer (Phase B). Everything here is
usable from the CLI and from tests; the later layers sit on top of it unchanged.

## Goal

From a clean checkout, with `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` in `.env`:

```bash
make ingest F="data/samples/Sample Contract.pdf"   # parse → chunk → embed → SQLite
make search Q="MFA for privileged access"          # vector / keyword / hybrid side by side
make chat                                          # multi-turn Q&A over the contract, every answer cited
make test                                          # offline: no key, no network
```

Every external HTTP call (Anthropic, OpenAI) goes through one `http_client.py`
with graceful failure and 3 retries on exponential backoff. Every log line goes
through one `logger.py`, structured JSON, with a trace id per request.

## Source of reuse

`/media/amirrezaslh/Dev/Interview/RAG_Mock/` — copied from the *working tree*
(its test files are uncommitted there). Package renamed `rag_mock` →
`contract_analyzer`. Where a file is copied verbatim the commit says so; where it
is adapted the commit body lists the change.

## Cross-cutting rules

- **One logger.** `src/contract_analyzer/logger.py` exposes `get_logger(name)`,
  `configure_logging(level, json_file)`, and `trace_context(trace_id=…)`. Every
  module does `from ..logger import get_logger; log = get_logger(__name__)`. No
  module imports `logging` directly (test enforces it). Console handler prints a
  compact human line; the file handler (`.run/app.jsonl`) writes one JSON object
  per line with `ts, level, logger, msg, trace_id, span_id, parent_span_id` and
  any `extra={}` fields. `span(name, **attrs)` context manager logs `span.start`
  / `span.end` with `latency_ms` and status — this is what Phase B's agents and
  the later KPI store will hang off, but in this phase it only logs.
- **One HTTP client.** `src/contract_analyzer/http_client.py` builds an
  `httpx.Client` whose transport retries on connection errors, timeouts, 408,
  429, and 5xx: 3 attempts, delays 1s → 2s → 4s (full jitter), honouring
  `Retry-After` when present. Non-retryable errors and exhausted retries raise
  `HttpFailure` with the method, URL, status, attempt count and elapsed time —
  never a raw traceback into the caller. The Anthropic and OpenAI SDK clients are
  constructed with `http_client=get_http_client()` and `max_retries=0`, so the
  SDKs' own retry loop is disabled and **our layer is the only one that retries**.
  `request(method, url, **kw)` is provided for any non-SDK call. Each attempt is
  logged (`http.retry` with attempt, wait, reason).
- **Offline tests.** `FakeEmbedder` + a stub Anthropic client keep the suite
  keyless; `httpx.MockTransport` tests the retry policy without a network.

## Commit sequence

| # | Commit | What |
|---|---|---|
| 1 | `chore: project scaffold` | `pyproject.toml` (deps: anthropic, openai, httpx, pymupdf, sqlite-vec, pydantic, pydantic-settings, tiktoken, python-dotenv; dev: pytest, ruff; optional `[local]` sentence-transformers), `Makefile` (ingest/search/chat/test/lint/fmt/logs), `.env.example`, `.gitignore` additions (`.run/`, `data/*.db`, `data/assets/`), `src/contract_analyzer/__init__.py`. Move `compliance_criteria.json` → `src/contract_analyzer/compliance/criteria.json` (used in Phase B); delete `prompts.json` stub. |
| 2 | `feat(core): logger with JSON file output and trace/span context` | `logger.py` + `tests/test_logger.py`. |
| 3 | `feat(core): retrying HTTP client for all external calls` | `http_client.py` + `tests/test_http_client.py` (MockTransport: 2×503 then 200 succeeds; 4×503 raises `HttpFailure` after 3 retries; 400 not retried; `Retry-After` honoured). |
| 4 | `feat(core): settings, SQLite store, chunk model, token counting` | Copy `config.py`, `db.py`, `schema.sql`, `models.py`, `tokens.py`. Config changes: `chunk_tokens=400`, `answer_model=claude-opus-5`, `log_file=.run/app.jsonl`, `http_timeout=60`, `http_retries=3`; drop `assets_dir` default only if unused (kept — figures still extractable). |
| 5 | `feat(parse): PyMuPDF parser (elements, blocks, outline, tables, figures)` | Verbatim copy of `parse/` with logger swap. `describe.py` prompt rewritten for contract figures (generic). |
| 6 | `feat(parse): synthesize section spine from numbered clause headings` | Contracts are Word→PDF with no `/Outlines`. `outline.synthesize_spine(elements)` builds the spine from detected heading elements (`^\d{1,2}\.\s`, `^Exhibit [A-Z]`) and from bold sub-clause prefixes on paragraphs (`^\d{1,2}\.\d{1,2}\s+Title.`, `^G\d+[A-Z]?\.\s`) so `assign_sections` gives every chunk a breadcrumb like `6. Identity… > 6.6 Password Management Standard`. `ParsedDocument.spine_source = "outline" | "headings" | "none"`. Tests on synthetic elements + on the sample contract (skipped if the PDF is absent). |
| 7 | `feat(ingest): chunker and idempotent pipeline, keeping exhibits and schedules` | Copy `ingest/chunker.py`, `pipeline.py`. Changes: never drop sections named exhibit/schedule/annex/appendix; `keep_references=True` default; table chunks are prefixed with their breadcrumb so BM25 finds "Password Management" rows in Exhibit G. |
| 8 | `feat(embeddings): openai, local and fake embedders through the shared HTTP client` | Copy `embeddings/`. `OpenAIEmbedder` builds `OpenAI(http_client=get_http_client(), max_retries=0)` and drops its own backoff loop (the transport owns it). |
| 9 | `feat(retrieval): hybrid vector+BM25 retrieval scoped to one document` | Copy `retrieval/`. Add `document_id` filter to vector, keyword and `retrieve()`; add `retrieve_by_section(conn, document_id, pattern)` for Phase B's router. |
| 10 | `feat(generation): cited answers over a contract with conversation history` | Copy `generation/` (client via `http_client`, `max_retries=0`), `prompts.json` rewritten for contracts (section-aware, no underwriting line), `answer()` takes `document_id`. |
| 11 | `feat(cli): ingest, search and chat scripts` | `scripts/ingest.py`, `scripts/search.py` (copied), new `scripts/chat.py` — a REPL with history, prints answer then citations as `[section · p.N] "quote"`; `/reset`, `/mode`, `/quit`. |
| 12 | `test: port the offline suite` | Copy tests, rename imports, add `conftest.py` with shared fixtures (populated DB via FakeEmbedder, stub Anthropic client), `test_no_direct_logging` guard. |
| 13 | `chore: sample contract fixture and parse report` | `data/samples/Sample Contract.pdf`; run `scripts/parse_report.py`; record counts in the implementation report. |
| 14 | `docs: foundation documentation` | `docs/parsing.md`, `docs/chunking.md`, `docs/retrieval.md`, `docs/generation.md`, `docs/logging-and-http.md` (adapted from RAG_Mock docs, contract examples); `plan_implement_docs/01_foundation_report.md`. |

## Design notes to be ready to defend

- **PyMuPDF, hand-written parser, no Unstructured/Docling**: 21-page Word-PDFs
  have real ruled tables and consistent fonts; a geometric parser is deterministic,
  fast (<1 s), and its failures are inspectable (`parse_report.py`). Figures are
  extracted but not described by default (no figures in the sample).
- **Section-aware chunking (400 tokens, 100 overlap, whole-element overlap)**:
  clauses are the unit of meaning in a contract; a chunk never crosses a section,
  and every chunk starts with its breadcrumb so both BM25 and the LLM see where
  it came from.
- **Hybrid retrieval + RRF**: compliance questions mix jargon ("TLS 1.2", "SAML",
  "PASS-02") that BM25 nails with paraphrase ("secure admin pathway" vs "bastion")
  that vectors catch. sqlite-vec + FTS5 keep it one file, zero infra.
- **Native Claude citations for chat**: quotes come from the API's citation
  feature — they cannot be invented. (Phase B's structured extraction cannot use
  this feature together with JSON output, so it verifies quotes itself.)
- **Retries in one place**: SDK retries disabled; the transport is the single
  policy, testable and logged.

## Verification checklist (checkpoint before Phase B)

- [ ] `make test` green, `make lint` clean, no module imports `logging` directly.
- [ ] `make ingest F="data/samples/Sample Contract.pdf"` → ~150–200 chunks, all
      with non-empty `section_path`; sections 6.6, 6.7, 7.2, 9.1–9.3, Exhibit G
      present as breadcrumbs.
- [ ] `make search Q="password rotation for break-glass credentials"` returns
      §6.6 and Exhibit G PASS rows in hybrid mode.
- [ ] `make chat`: "Does the vendor have to use MFA?" answers with citations to
      §6.2 / Exhibit G; a follow-up "for which accounts?" uses history.
- [ ] Pull the network: retries are visible in `.run/app.jsonl` and the CLI
      prints a one-line `HttpFailure`, not a traceback.
