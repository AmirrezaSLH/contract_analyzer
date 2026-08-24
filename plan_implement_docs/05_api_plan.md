# Step 12 — the HTTP API: one backend, four consumers

**Status: draft for review, 2026-08-24.** Supersedes steps 24–26 of
`00_overall_plan.md` where they conflict. Assumes step 10 (generation) is
in, and that the evaluator (`04_02_evaluator_agent.md`), the document-level
runner and the metrics store land *before or alongside* this step — the API
wraps them, it does not contain them.

**First iteration: one document per session, many in the store.** Each
`POST /documents` mints a new `document_id`. A UI tab, an MCP tool call, or
a connector request uploads one file and then binds analysis and chat to
that id. Retrieval, chat tools, and the criterion runner always pass
`document_id` — never `ALL_DOCUMENTS` — so vector and keyword search cannot
cite another contract. `GET /documents` lists every stored file by id and
name so a client can pick the one it uploaded.

## Who calls it

The API is the only process that touches the database and the model. Every
other surface is a client of it, and that is the point of building it once:

| Consumer | Uses | Needs from the API |
|---|---|---|
| **Streamlit UI** (Phase C) | upload → status → results; KPI page; chat | multipart upload, a pollable job, the full report JSON, metrics JSON, a streamed chat |
| **MCP server** (Phase C) | the same operations as tools, over HTTP, stateless | small responses by default, stable ids as state handles, errors a model can act on, a trace id it can pass through |
| **External connector** (assignment §3.3) | a third-party chat app calling the OpenAPI spec | the OpenAPI document *is* the deliverable: good summaries, examples, error schemas, an API key |
| **CLIs / tests** | `scripts/*.py` keep calling the library directly | nothing — but the API's job runner must be the same function the CLI calls |

So the rule: **the API contains no logic that the CLI does not have.** Every
handler is `parse request → call a library function → shape the response`.
The library functions that do not yet exist are named in
[Prerequisites](#prerequisites).

## Design decisions

1. **Jobs, not long requests.** A five-criterion run is 60–180 s on
   `claude-opus-5`; that is past any browser, proxy or MCP client timeout.
   `POST /analyses` returns `202` with an id in under a second; the client
   polls `GET /analyses/{id}` or subscribes to `GET /analyses/{id}/events`.
   The id is the state handle — the server keeps nothing per client.
2. **Upload mints an id; analysis and chat are bound to it.** Every
   `POST /documents` writes a new row and returns a unique `document_id`
   (SQLite integer). The API stores the file as
   `RAW_DIR/<uuid>-<original-name>.pdf` so two uploads of the same bytes are
   still two documents — `ingest_file` keys uniqueness on path, and a unique
   path is how we get a unique id. `POST /analyses` and `POST /chat` require
   that `document_id`; there is no implicit "current document". A one-shot
   `POST /analyses` with a file is the UI convenience: ingest, then queue
   against the new id. Isolation is a library invariant, not an API extra:
   `retrieve()`, `chat()`, and `analyze_document()` already take
   `document_id`, and the API never passes `ALL_DOCUMENTS`.
3. **Chat streams over SSE, and only chat.** It is the one interaction
   where latency is felt token by token. Analysis progress uses SSE too but
   only as an *optional* upgrade over polling; polling is the contract.
4. **One thread pool, bounded, owned by the app.** `ThreadPoolExecutor(2)`
   on `app.state`; each job opens its own `get_db(same_thread=False)`
   connection and closes it. SQLite serialises writes; two concurrent
   analyses is the honest ceiling for a single-file store, and the
   `queued` state is what a third request sees. No Celery, no Redis: a
   local demo does not need a broker, and the runner is one function
   behind an interface that a broker could implement later.
5. **The report on disk is the report over the wire.** `ComplianceResult`
   already serialises; `AnalysisReport` (the five results plus totals) is
   persisted as JSON in the `runs` row the metrics store writes. `GET
   /analyses/{id}` reads it back — there is no second schema.
6. **Trace id in, trace id out.** Every request runs under
   `trace_context()`. An incoming `X-Trace-Id` is honoured (the MCP server
   and the UI send one); the response carries `X-Trace-Id` either way, and a
   job keeps the trace id of the request that started it, so one Claude
   Desktop tool call, its API request, the five criterion runs and their
   tool calls share one id in `.run/app.jsonl`.
7. **API key, static, optional.** `X-API-Key` checked against `API_KEY` when
   set; unset means open, which is the local demo. The OpenAPI spec declares
   the scheme so the connector story is complete; production would be
   OAuth 2.1 with per-tenant `document_id` scoping, and the docs say so.
8. **No framework beyond FastAPI + uvicorn.** Both are already implied by
   the Docker entrypoint. `sse-starlette` for the two streams. No
   `fastapi-users`, no `slowapi`, no ORM.

## Endpoints

Base path `/` (a `/v1` prefix is one line to add and the MCP client would
be the only caller to update; deferred until a second version exists).

### Health & reference

| Method | Path | Returns |
|---|---|---|
| `GET` | `/health` | `{status, version, db: ok, embedder: provider, answer_model, key_present: bool}` — the Docker healthcheck already calls this |
| `GET` | `/criteria` | `Criterion[]` from `get_criteria()` — id, requirement, question, sub_requirements |

### Documents

| Method | Path | Returns |
|---|---|---|
| `POST` | `/documents` (multipart `file`) | `201 Document` — `{document_id, filename, pages, chunks, spine_source, elapsed_s}` |
| `GET` | `/documents` | `[{document_id, filename}]` newest first — the list a client uses to bind a session |
| `GET` | `/documents/{id}` | `Document`, plus `analyses: [{analysis_id, status, created_at}]` |
| `GET` | `/documents/{id}/sections` | `[{path, page_display, chunks}]` — the outline, for the UI's section picker and the MCP `get_section` tool |
| `DELETE` | `/documents/{id}` | `204`; removes that document's chunks, vectors, FTS rows, and raw file; `409` while an analysis on it is running |

`POST /documents` saves under `RAW_DIR/<uuid>-<original-name>.pdf`, then
`ingest_file(path, conn, embedder, settings)`. The original filename is
what `GET /documents` returns; the uuid is only on disk so two sessions
cannot collide. Non-PDF → `415`; empty → `422`; `EmbedderUnavailable` →
`503` with the message that names the `.env` key. Unknown `{id}` → `404`.

### Analyses (jobs)

| Method | Path | Returns |
|---|---|---|
| `POST` | `/analyses` (JSON `{document_id, criteria?: [id]}` **or** multipart `file`) | `202 AnalysisStatus` — `{analysis_id, document_id, status: queued, created_at}`; missing/unknown `document_id` → `422` / `404`; multipart file = ingest then queue on the new id |
| `GET` | `/analyses/{id}` | `AnalysisStatus` — `{analysis_id, document_id, status, stage, progress: {done, total}, criteria: [{id, status, state?, confidence?}], report?: AnalysisReport, error?, trace_id, started_at, completed_at, totals: {latency_s, cost_usd, tokens}}` |
| `GET` | `/analyses/{id}/events` | SSE: `status`, `criterion` (one per finished criterion, with its result), `tool_call` (from `on_event`), `done` / `error`; closes after `done` |
| `GET` | `/analyses` | `AnalysisStatus[]` without `report`, newest first; `?document_id=` required to keep the list scoped to one contract |
| `POST` | `/analyses/{id}/cancel` | `202`; sets a flag the runner checks between criteria — a running criterion finishes, the rest are skipped |

Status machine: `queued → running → done | failed | cancelled`. `stage`
is human text (`ingesting`, `criterion 3/5: data_in_transit`, `evaluating`).
`report` is present only on `done` (and, partial, on `cancelled`).
`?detail=summary` on `GET /analyses/{id}` omits quotes and rationale — the
MCP default, because a full report is a lot of context.

### Chat

| Method | Path | Returns |
|---|---|---|
| `POST` | `/chat` (JSON `{document_id, question, history?: [{role, content}], stream?: true}`) | `stream=true` (default): SSE `text` deltas, `tool_call` events, one `citations` event, `done` with usage/cost; `stream=false`: `AnswerResult` as one JSON |

`document_id` is required. Unknown id → `404` with hint to `GET /documents`.
`chat()` is called with that id; tools cannot search another contract.
`history` is the client's — the API is stateless; the UI keeps the
transcript in session state and the MCP server passes what the model gave
it. `chat()` already caps it at 8 messages.

### Metrics (for the KPI page; read-only over the metrics store)

| Method | Path | Returns |
|---|---|---|
| `GET` | `/metrics/summary?window=24h` | the real-time tiles: active jobs, runs, failures, p50/p95 latency, cost, quote-verification rate, evaluator accept rate, mean confidence, `needs_review` rate, cap rate |
| `GET` | `/metrics/timeseries?bucket=1h&window=7d` | per bucket: runs, p50/p95, cost, mean confidence, state distribution |
| `GET` | `/metrics/runs?limit=50` | the runs table |
| `GET` | `/metrics/runs/{id}/spans` | the span tree for the waterfall |

Exact KPI selection is the KPI plan's; the API exposes whatever
`MetricsStore.summary/timeseries/runs/spans` return, unchanged.

## Response models (`api/schemas.py`)

Pydantic, and where a library type exists it is reused, not mirrored:
`ComplianceResult`, `Criterion`, `AnswerResult` (minus the `Evidence`
object; citations resolved to `{evidence_id, quote, title, page_display,
chunk_id, start, end}`). New: `Document`, `AnalysisStatus`, `AnalysisReport`
(`analysis_id, document_id, filename, results: ComplianceResult[], totals,
cross_criterion_notes, created_at, completed_at`), `ChatRequest`, `Error`.

Every error is `{error: {code, message, hint?}}` with a stable `code`
(`document_not_found`, `analysis_running`, `no_api_key`, `embedder_unavailable`,
`unsupported_media_type`, `validation`). `hint` is the sentence a model can
act on ("call GET /documents to list document_id and filename"). One exception handler maps
`AnswerUnavailable` / `EmbedderUnavailable` → `503`, `HttpFailure` → `502`,
`ModelMismatch` → `409`, `KeyError` on ids → `404`, pydantic → `422`.

## Module layout

```
src/contract_analyzer/api/
  __init__.py
  main.py        create_app(settings) -> FastAPI; lifespan opens the pool and
                 the metrics store; app = create_app() for uvicorn
  deps.py        get_settings, get_conn (per-request, closed after), require_key,
                 trace middleware
  schemas.py     response/request models, Error
  routes/
    health.py  documents.py  analyses.py  chat.py  metrics.py
  jobs.py        JobRunner: submit(document_id, criteria, trace_id) -> id;
                 status(id); cancel(id); the worker function
  sse.py         event helpers: sse(event, data), the keepalive comment
```

`JobRunner` is a class with an in-memory `dict[id, JobState]` **and** the
`runs` row: the dict is the live view (stage, progress, cancel flag), the
row is the durable one. On startup, rows still `running` from a previous
process are marked `failed: interrupted` — a restart must not show a job
that will never finish.

### The worker

```python
def _run(job):
    with trace_context(job.trace_id), span("api.analysis", analysis_id=job.id):
        conn = get_db(settings, same_thread=False)
        try:
            analyze_document(job.document_id, conn, embedder, settings, client,
                             criteria=job.criteria, on_event=job.emit,
                             cancelled=job.cancelled)
        finally:
            conn.close()
```

`analyze_document()` is the prerequisite library function (five
`analyze_criterion` calls in their own inner pool of 5, then the evaluator's
cross-criterion pass, then `MetricsStore.end_run`). `on_event` is the same
callback the CLI prints; the API fans it into the SSE subscribers and the
progress dict.

Concurrency inside the worker: the inner pool of 5 criterion runs shares
one connection — SQLite reads are fine concurrently on one connection with
`check_same_thread=False`, and criterion runs only read. Writes (the run row,
the spans) happen on the worker thread after the pool joins.

## Streaming

Both streams are `sse-starlette` `EventSourceResponse` over a
`queue.Queue` the worker or the `on_text` callback pushes into. A comment
frame every 15 s keeps proxies from closing an idle analysis stream. The
chat stream ends with `done` carrying `{usage, cost_usd, stop_reason,
ended_by}`; the client-side transcript appends `text` deltas and renders the
`citations` event once. `HttpFailure` mid-stream becomes an `error` event
and a clean close, never a broken connection.

## Settings (`config.py`)

| Setting | Default | |
|---|---|---|
| `api_key` | unset | `X-API-Key`; unset = open |
| `api_workers` | 2 | analysis jobs in flight |
| `api_max_upload_mb` | 25 | `413` above |
| `api_cors_origins` | `[]` | the UI is a different origin only outside Docker |
| `api_keepalive_seconds` | 15 | SSE comment cadence |

`CA_API_URL` is the *clients'* setting (UI, MCP), already in compose.

## Docker

`Dockerfile` runtime stage gains `fastapi`, `uvicorn[standard]`,
`sse-starlette`, `python-multipart` (a new `[api]` extra in `pyproject`);
the `api` service gets `restart: unless-stopped` and the comment about the
missing module goes. The healthcheck already targets `/health`.

## Trace propagation, end to end

```
UI / MCP ── X-Trace-Id ──► middleware: trace_context(id or new) ──► handler
                                   │
                                   └─► JobRunner.submit(trace_id) ──► worker: trace_context(trace_id)
                                                                          └─► analyze_document → spans
response ◄── X-Trace-Id ──┘
```

The MCP server mints one id per tool call and sends it; the UI mints one per
upload and shows it beside the run, which is the demo's "here is the same
id in the log" moment.

## Prerequisites

Library functions the API calls that do not exist yet, in the order they
are needed:

1. `compliance/report.py`: `analyze_document(document_id, conn, embedder,
   settings, client, *, criteria=None, on_event=None, cancelled=None) ->
   AnalysisReport` — the runner (overall plan step 20). **Blocks `POST
   /analyses`.**
2. `metrics/store.py`: `MetricsStore` with `runs` / `spans` /
   `criterion_results` (step 22) — **blocks `/metrics/*`** and the durable
   half of job status. The API can ship with jobs in memory only and add the
   store in the same week; the endpoints are stubs until then.
3. `db.py`: `delete_document(conn, document_id)`, `list_documents(conn)`
   (`id`, `filename`, newest first), `document_sections(conn, document_id)`
   — small. Retrieval already filters by `document_id`; the API must not
   call `retrieve(..., document_id=ALL_DOCUMENTS)`.
4. The evaluator — **not blocking**: the API returns whatever
   `ComplianceResult` carries; `evaluator` fields appear when they exist.

## Commit sequence

| # | Commit | What |
|---|---|---|
| 12a | `feat(db): list, sections and delete for documents` | `db.py`: `list_documents` (id, filename), sections, delete |
| 12b | `feat(compliance): analyze_document runner and the analysis report` | `report.py`, `AnalysisReport`; `scripts/analyze.py`; `make analyze` — **first live run** |
| 12c | `test: the runner -- parallel criteria, cancel, partial report` | scripted API, five criteria |
| 12d | `feat(api): app, health, criteria, documents` | `main.py`, `deps.py`, `schemas.py`, `routes/{health,documents}.py`; `[api]` extra |
| 12e | `feat(api): analysis jobs -- submit, poll, events, cancel` | `jobs.py`, `sse.py`, `routes/analyses.py` |
| 12f | `feat(api): streamed cited chat` | `routes/chat.py` |
| 12g | `feat(api): metrics endpoints over the store` | `routes/metrics.py` (after the store lands) |
| 12h | `test: api -- upload, jobs, sse, chat, errors, trace ids` | `TestClient`, fake embedder, scripted model API |
| 12i | `chore(docker): api service live` | `Dockerfile`, compose `restart`, extra |
| 12j | `docs(api): endpoints, jobs, streaming, the connector spec` | `docs/api.md`; `architecture.md` rows; export `openapi.json` to `docs/` as the §3.3 artefact |

Estimate: 12a–12c 2 h, 12d–12f 2.5 h, 12g 0.5 h, 12h 1.5 h, 12i–12j 1 h.
**Cut order if over budget:** `/events` SSE for analyses (polling stays),
`DELETE /documents`, cancel.

## Tests (`tests/test_api.py`)

`fastapi.testclient.TestClient` over `create_app(settings)` with
`EMBEDDING_PROVIDER=fake`, a temp database, and the model reached through
the scripted SSE transport from `conftest.py` — no network, no key.

* `GET /health` reports `key_present: false` without a key; `/criteria`
  lists five with sub-requirements.
* upload the sample → `201` with `document_id` and `filename`; upload the
  same bytes again → `201` with a *different* `document_id`; `GET
  /documents` returns both `{document_id, filename}`; a `.txt` → `415`;
  oversize → `413`.
* `POST /analyses` without `document_id` → `422`; unknown id → `404` with
  hint. With a valid id the job runs on the pool with a scripted model;
  `GET` shows `running` with progress then `done` with a report whose five
  results validate as `ComplianceResult`; `?detail=summary` has no quotes.
* two documents ingested; analysis and chat on A never return chunks from B
  (retrieval `document_id` filter).
* `/events` yields `status`, five `criterion`, `done`, then closes.
* cancel after criterion 1 → `cancelled` with a one-result partial report.
* a third submission while two run → `queued`; runs after one finishes.
* chat `stream=true` → `text` deltas concatenate, one `citations` event,
  `done` with usage; `stream=false` → the same as one JSON; unknown
  `document_id` → `404` with `hint`.
* no key → `503 no_api_key` on chat and analyses, `201` on upload (ingest
  needs no answer key).
* `X-Trace-Id` in → same id on the response and in every log line of the
  job; none in → one minted and returned.
* `API_KEY` set → `401` without the header, `200` with it; `/health` open.
* rows `running` at startup → `failed: interrupted`.
* the OpenAPI document has a description on every operation and the
  `Error` schema on every 4xx/5xx.

## Acceptance

- [ ] `make test` green, `make lint` clean; `api/` imports nothing from
      `ui/` or `mcp/`.
- [ ] `make docker-up` → `/health` green; upload the sample through
      `/docs`; `GET /documents` shows its `document_id` and filename;
      `POST /analyses` with that id; poll to `done`; the report validates.
      A second upload has a different id; chat on the first never cites it.
- [ ] One `X-Trace-Id` from the request appears on every line of that
      analysis in `.run/app.jsonl`, including the tool-call spans.
- [ ] `/chat` streams a cited answer for the MFA question with `p.4`.
- [ ] `docs/openapi.json` exported and linked from `architecture.md` as the
      §3.3 connector specification.
- [ ] No handler contains logic the CLI does not have.

## Open questions

1. **`/v1` now or later?** Recommendation: later; one caller (MCP) to update.
2. **Should `POST /analyses` accept a file directly?** Recommendation: yes
   as a convenience — the UI's single "upload and analyse" button — but the
   two-step path is the documented one.
3. **Persist job state only in the metrics `runs` row, or also a `jobs`
   table?** Recommendation: the `runs` row plus the in-memory dict; a
   separate table is a second source of truth for the same fact.
4. **Rate limiting?** Not for a local demo; the pool size is the limit.
   Document it as the production gap alongside auth.
5. **Should the API embed on upload synchronously?** Yes: ingest is ~3 s
   with OpenAI embeddings and the UI wants a `document_id` before it can do
   anything. If a 200-page contract appears, upload becomes a job too, with
   the same runner — that is why `JobRunner` is generic over the worker
   function.
6. **Hash-idempotent ingest vs unique id per upload?** Recommendation:
   unique id per `POST /documents` (uuid in the stored path). Content-hash
   skip inside `ingest_file` still applies if the same path is ingested
   twice (CLI); the API never reuses a path. Sessions stay isolated even
   when two people upload the same PDF.
