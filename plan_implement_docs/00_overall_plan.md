# Contract Analyzer — reuse RAG_Mock, rebuild with granular commits

## Context

`contract_analyzer/` is a near-empty repo (README stub, `.gitignore`, `compliance_criteria.json` with the 5 Table-1 questions, a `prompts.json` stub, `AGENTS.md`). The assignment (`assignment_details/assignment_description.md`) needs, in 8–12h: PDF upload UI → parse (text/tables) → **Agentic RAG with Router + Evaluator agents** → validated JSON per compliance question → **KPI dashboard** (real-time + historical) → structured JSON logs with trace/span IDs → **MCP/connector** design + OpenAPI → docs + slide outline. Bonus: chat over the contract.

`/media/amirrezaslh/Dev/Interview/RAG_Mock/` is a hand-written (no-LangChain) RAG system, ~9.6k lines incl. ~154 offline tests: PyMuPDF parser → chunker → embeddings (openai/local/fake) → SQLite (sqlite-vec + FTS5) → hybrid RRF retrieval → Anthropic generation with native citations → FastAPI SSE → Streamlit. It covers ~70% of the new project; the rest (agents, structured output, evaluator, observability, KPI, MCP, jobs) is net-new.

**Decisions confirmed with the user:** Anthropic API for agents; OpenAI `text-embedding-3-small` embeddings; hand-rolled typed state machine (no LangGraph); Streamlit UI; **copy modules** from RAG_Mock's *working tree* (it has uncommitted test files) into `src/contract_analyzer/`.

**Commit rules** (from `AGENTS.md`): author `slh.amirreza@gmail.com`, **no AI co-author trailer**. Conventional-commit style (`feat(scope): …`) matching RAG_Mock history — one vertical slice per commit.

### Verified facts that shape the plan
- Sample contract (`assignment_details/Sample Contract.pdf`): 21 pages, Word→PDF, **no `/Outlines`, no page labels**. Running RAG_Mock's `parse_pdf` on it already yields **51 headings** (all numbered sections `1.`–`21.` + `Exhibit A–G`, by font size) and **42 ruled tables** — but `sections == []` so every chunk breadcrumb is blank. Sub-clauses like `6.6 Password Management Standard.` are bold inline runs at paragraph start (never headings by size).
- `generation/answer.py` documents that `output_config.format` (structured output) **returns 400 when combined with citations** → extraction (JSON via `client.messages.parse(output_format=Model)`) and chat (citations) must be separate code paths; hallucination detection = deterministic verbatim-quote verification against the evidence we sent.
- `retrieve()` has no `document_id` filter — analysis must be scoped to one uploaded contract.
- Chunker drops `references/bibliography` and front-matter sections — wrong for contracts where Exhibits *are* the evidence.
- `POST /documents` is synchronous; the analysis needs a background job + status endpoint.

## Reuse map

| RAG_Mock module | Reuse | Change |
|---|---|---|
| `config.py`, `db.py`, `schema.sql`, `models.py`, `tokens.py` | copy | add agent/model/log settings; append metrics tables |
| `parse/*` (7 files) | copy | add heading-based spine fallback + clause-number regex; retune `describe.py` prompt (figures not needed for demo — keep `extract_figures=False` default) |
| `ingest/chunker.py`, `pipeline.py` | copy | never drop exhibits/schedules; breadcrumb on table chunks |
| `embeddings/*` (5 files) | copy verbatim | — |
| `retrieval/*` (4 files) | copy | `document_id` filter + `retrieve_by_section` |
| `generation/client.py, prompts.py, blocks.py, answer.py, prompts.json` | copy | contract-domain system prompt; `document_id` passthrough; reuse `PromptLibrary` for agent prompts |
| `api/main.py, deps.py, schemas.py, routes_chat.py, routes_docs.py` | copy | add analyses/metrics routers, job executor |
| `ui/backend.py` (Backend protocol, Mock/Http), `ui/app.py` | copy/adapt | new pages; replace thesis/insurance skins & fixtures |
| `run.bash`, `stop.bash`, `Makefile`, `.env.example`, `pyproject.toml` | copy | rename; drop corpus-fingerprint ingest; add `mcp`, `logs`, `analyze` targets |
| `tests/*`, `scripts/*` | copy | rename imports; skip corpus-dependent parse tests |
| Not reused | — | thesis fixtures, `_CANNED` insurance answers, `ANSWER_MODELS` hardcode, `work_info/` |

## Target layout

```
src/contract_analyzer/
  config.py db.py schema.sql models.py tokens.py
  parse/ ingest/ embeddings/ retrieval/ generation/          (copied, adapted)
  compliance/{schemas.py, criteria.py, criteria.json}        (new)
  agents/{state.py, llm.py, router.py, extractor.py, evaluator.py, runner.py, prompts.json}  (new)
  observability/{logging.py, tracing.py, metrics.py, pricing.py}                             (new)
  api/{main,deps,schemas,routes_chat,routes_docs,routes_analyses,routes_metrics,jobs}.py
  mcp/server.py                                              (new, FastMCP)
ui/app.py ui/backend.py ui/pages/{1_Analyze,2_Chat,3_KPI_Dashboard}.py ui/fixtures/sample_report.json
scripts/{ingest,search,serve,dump_parse,parse_report,analyze,mcp_server}.py
tests/ docs/ data/samples/ run.bash stop.bash Makefile .env.example pyproject.toml
```

## Commit sequence (≈35 commits, ~10.5h)

### Phase 0 — Bootstrap & copy (~1h) → `make test` green
1. `chore: project scaffold (pyproject, Makefile, env example, run/stop scripts)` — copy from RAG_Mock, rename `rag_mock`→`contract_analyzer`; deps + `mcp>=1.2`, `pandas`, `plotly`; drop unused `pypdf/python-docx/pyyaml/httpx` (keep `httpx` for MCP client), add `requests`; package-data `agents/*.json`, `compliance/*.json`. Move `compliance_criteria.json` → `src/contract_analyzer/compliance/criteria.json`; delete `prompts.json` stub. `run.bash`: keep venv bootstrap / API health wait / Streamlit; remove fingerprint-ingest block.
2. `feat(core): config, db, schema, models, tokens` — `config.py` adds `router_model/extractor_model/evaluator_model` (default `claude-opus-5`), `agent_effort="medium"`, `max_retries=2`, `analysis_top_k=6`, `criteria_path`, `agent_prompts_path`, `log_file=.run/app.jsonl`; `chunk_tokens=400`.
3. `feat(parse): PyMuPDF parser modules` — verbatim copy.
4. `feat(ingest): chunker and ingest pipeline` — verbatim copy.
5. `feat(embeddings): embedder providers (openai, local, fake, guard)` — verbatim.
6. `feat(retrieval): hybrid RRF retrieval` — verbatim.
7. `feat(generation): Anthropic client, prompt library, cited answers` — contract-domain `prompts.json` system prompt (no underwriting line).
8. `test: port offline test suite and scripts` — from working tree; `skipif` corpus PDFs absent; update `test_prompts` content assertion. Verify `make test`.

### Phase 1 — Parser generalisation (~1.5h)
9. `feat(parse): synthesize section spine from numbered headings when the PDF has no outline` — `outline.synthesize_spine(elements)` in `parse_pdf` when `build_spine` empty: L1 `^\d{1,2}\.\s+[A-Z]` on heading elements and `^Exhibit\s+[A-Z]\b`; L2 `^(\d{1,2}\.\d{1,2})\s+([A-Z][^.]{2,60})\.` on paragraph prefixes (element stays a paragraph; spine entry gets its bbox y) and `^G\d+[A-Z]?\.` under an Exhibit. `assign_sections` unchanged. Test on a synthetic element list.
10. `feat(ingest): keep exhibits and schedules; breadcrumb on table chunks` — `keep_references=True` default; `_NEVER_DROP={"exhibit","schedule","annex","appendix"}` guard in `_select`; table chunk content prefixed with breadcrumb.
11. `feat(retrieval): scope retrieval to one document` — `document_id` kwarg through `retrieve()`; keyword joins `chunks`; vector uses `chunk_id IN (SELECT id FROM chunks WHERE document_id=?)` (fallback over-fetch ×4 + filter); `retrieve_by_section(conn, document_id, pattern)` (LIKE on `section_path`). Thread `document_id` through `answer()`.
12. `chore(scripts): parse report over the sample contract` — copy contract to `data/samples/`; run `parse_report`; record counts in `docs/parsing-notes.md`; confirm 6.6/6.7/7.2/9.x/Exhibit G chunks carry the right section. Adjust regexes if not.

### Phase 2 — Compliance schema & agents (~3h) → `scripts/analyze.py` end-to-end
13. `feat(compliance): pydantic result schema and criteria loader` — `ComplianceState` enum; `Quote(text, section_ref, page_label, chunk_id, verified)`; `SubCriterionFinding(id, requirement, status: met|partial|missing, quote_indexes)`; `EvaluatorVerdict(verdict: accept|revise|fallback, coverage, consistent, quotes_claimed, quotes_verified, hallucination_flags, raw_confidence, calibrated_confidence, notes)`; `ComplianceResult(criterion_id, compliance_question, compliance_state, confidence, relevant_quotes, rationale, sub_criteria, evaluator, attempts, needs_review)`; `UsageTotals`; `AnalysisReport(analysis_id, trace_id, document_id, filename, status, results, totals, created_at, completed_at, error)`. `load_criteria()` → `Criterion(id, name, description, options)`.
14. `feat(agents): typed analysis state machine and runner skeleton` — `Phase` enum ROUTE→EXTRACT→EVALUATE→(RETRY→EXTRACT)|DONE|FAILED; `CriterionState`, `AnalysisState`; runner takes injectable step functions (tests drive with fakes).
15. `feat(agents): prompt library for router, extractor, evaluator` — `agents/prompts.json` loaded via existing `PromptLibrary`. Router: decompose each criterion into 3–7 sub-requirements with `search_queries[]`, `section_hints[]`, `evidence_kind: clause|table`. Extractor: numbered evidence blocks `[E1] section/page/text`; quotes verbatim ≤200 chars with `evidence_id`. Evaluator: rubric for completeness/consistency/unsupported claims; calibration guide.
16. `feat(agents): structured LLM call helper with usage and cost capture` — `call_structured(client, model, system, user, output_model, effort) -> (parsed, Usage)` via `client.messages.parse(output_format=…, thinking adaptive, output_config effort)`; `AgentCallError` on refusal/validation; `pricing.cost_usd(model, in, out)` table (opus-5 5/25, sonnet-5 3/15, haiku-4.5 1/5, embedding 0.02). Stub client in tests.
17. `feat(agents): router agent — decompose criteria and select evidence` — one LLM call for all 5 criteria → `RoutePlan`s; deterministic dispatch: hybrid `retrieve(query, document_id, top_k)` per sub-criterion + `retrieve_by_section` for hints; dedupe; cap 12 chunks/criterion; partition clause vs table evidence.
18. `feat(agents): extractor agent with table-row parser` — evidence blocks; table chunks pre-filtered to relevant rows (`table_rows_matching(payload, ids)`); returns `ExtractionDraft`; accepts `critic_feedback` on retry.
19. `feat(agents): evaluator agent — quote verification, consistency rules, calibration` — deterministic pass: `verify_quote` (NFKC + whitespace/quote-fold + casefold substring); state/finding consistency (Fully⇒all met, Non⇒none met); coverage. Then critic LLM call → `CriticVerdict`. `calibrated = min(model_conf, critic_conf) × verified_ratio × (0.5+0.5×coverage)`. Verdict accept / revise (unverified quote, coverage<0.8, inconsistency) / fallback (`needs_review=True`, confidence cap 0.5). Fully unit-tested offline.
20. `feat(agents): wire router→extractor→evaluator→retry; analyze CLI` — `analyze_document(document_id, conn, embedder, settings, client, on_event)`; extractors parallel via `ThreadPoolExecutor(5)`; RETRY widens retrieval (top_k×2 + critic's missing sub-criteria as queries). `scripts/analyze.py path.pdf` → JSON; `make analyze F=…`. **First real run on the sample contract.**

### Phase 3 — Observability (~1.25h)
21. `feat(observability): structured JSON logging with trace/span context` — contextvars `trace_id/span_id/run_id`; `JsonFormatter` (`ts, level, logger, msg, trace_id, span_id, parent_span_id, run_id, agent, criterion_id, attempt, extra`); `span(name, **attrs)` ctx manager logging `span.start/end` with latency/status/exception; console + `.run/app.jsonl`; `make logs` tails it.
22. `feat(metrics): SQLite runs/spans/criterion_results store` — append to `schema.sql`: `runs` (status, latency, tokens, cost, retries, confidence stats, quotes claimed/verified, verdict counts, `report_json`), `spans` (run_id, parent, name, agent, criterion_id, attempt, latency, model, tokens, cost, attrs JSON), `criterion_results` (per run×criterion state/confidence/verdict/attempts/coverage). `MetricsStore` with `start_run/end_run/record_span/record_criterion/summary(window)/runs(limit)/timeseries(bucket)`.
23. `feat(agents): emit spans and token/cost/verdict metrics from every agent step`.

### Phase 4 — API (~1h)
24. `feat(api): FastAPI app, upload and document-scoped chat` — copy; `ChatRequest.document_id`; OpenAPI = connector spec.
25. `feat(api): background analysis jobs — POST /analyses, GET /analyses/{id}` — multipart upload → run row `queued` → `ThreadPoolExecutor(2)` on `app.state`, own `get_db(same_thread=False)` conn: ingest → analyze → persist report. `GET /analyses/{id}` → `{status, stage, progress, report?, error?}`; `GET /analyses`. Optional `GET /analyses/{id}/events` SSE (cut if late).
26. `feat(api): metrics endpoints` — `/metrics/summary?window=`, `/metrics/runs`, `/metrics/timeseries?bucket=`, `/metrics/spans/{run_id}`.

### Phase 5 — UI + KPI (~1.5h)
27. `feat(ui): analysis page — upload, live status, structured results` — extend `Backend` protocol (`start_analysis`, `get_analysis`, `metrics_*`); Mock returns `ui/fixtures/sample_report.json` (keyless demo path); 2s polling with `st.status`; per-criterion state badge, confidence bar, verified quotes with section/page, rationale, evaluator verdict/attempts; JSON download.
28. `feat(ui): KPI dashboard page — real-time tiles and historical charts` — tiles: active jobs, last-run latency, cost, quote-verification rate, evaluator accept rate, mean confidence, error rate (auto-refresh toggle). Historical (plotly): latency p50/p95/day, cost/day, state distribution per criterion, confidence histogram, retries/run, runs table → span waterfall. Thresholds from a shared `KPI_THRESHOLDS` dict.
29. `feat(ui): chat over the analysed contract (bonus)` — document selector + reuse chat UI.

### Phase 6 — MCP (~0.5h)
30. `feat(mcp): FastMCP server exposing analyze_contract, get_analysis, ask_contract, list_documents` — thin httpx clients over the API (`CA_API_URL`), stdio transport; `make mcp`; Claude Desktop config example; offline test over `list_tools()`.

### Phase 7 — Docs (~1h)
31. `docs: README` — single `./run.bash`, `.env` keys, keyless path (`EMBEDDING_PROVIDER=fake` + `make ui-mock`), `make logs`, measured cost/latency table, dependency licences (PyMuPDF AGPL note).
32. `docs: design document` — Mermaid pipeline, state machine, sequence; framework/protocol/retry justification; citations-vs-format constraint.
33. `docs: connector and MCP design` — diagram (chat client → MCP → FastAPI → agents/DB), `/openapi.json`, API-key auth + tenant-scoped document ids, `analysis_id/document_id` as state handles, multi-turn via `history`.
34. `docs: KPI catalogue with thresholds/alert actions; slide deck outline`.
35. `chore: final run.bash polish, .env.example, recorded sample report fixture`.

**Cut order if over budget:** 25's SSE events → 29 → 28's span waterfall → 12 → 35.

## KPIs (defend in interview)
Real-time: e2e latency (p50<90s, alert p95>180s); cost/analysis (<$0.40, alert >$1); quote verification rate (≥95%, alert <90% — direct hallucination signal); evaluator revise rate (<30%, alert >50%) and fallback count (alert ≥1); mean calibrated confidence ≥0.75 / low-confidence share <20%; LLM error/429/refusal rate <2%; queued/failed jobs; retrieval health (empty retrievals=0, chunks/doc ≥20 for 20pp).
Historical: latency & cost trends, state distribution per criterion (same doc hash changing state across runs = drift), retries/run, per-agent latency/token share, confidence vs `needs_review`.

## Verification
- After #8: `make test` green (offline, FakeEmbedder + stub client), `make lint`.
- After #12: `scripts/parse_report.py data/samples/…` shows 51 headings, 42 tables, non-empty `section_path` on chunks for 6.6/7.2/9.1/Exhibit G.
- After #20: `make analyze F="data/samples/Sample Contract.pdf"` prints 5 validated `ComplianceResult`s; all quotes `verified=True`; check q5 ≈ Fully Compliant like the sample output.
- After #23: `tail -f .run/app.jsonl | jq` shows nested spans with shared `trace_id`.
- After #26: `curl -F file=@… /analyses` → poll `/analyses/{id}` to `done`; `/metrics/summary` reflects the run; `/openapi.json` valid.
- After #28: `./run.bash` → upload in UI → results table → KPI page shows the run in tiles and history; `make ui-mock` works with no keys.
- After #30: `make mcp` + `tests/test_mcp.py` lists 4 tools with schemas.
- Every commit: `make test` passes before committing.
