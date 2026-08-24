# Step 12 — the HTTP API: one backend, four consumers

**Status: revised after review, 2026-08-24.** Supersedes steps 24–26 of
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

1. **Jobs, not long requests.** Measured on the sample contract
   (`.run/analyze_sample.jsonl`, 2026-08-24, `claude-opus-5`):

   | criterion | latency | cost |
   |---|---|---|
   | password_management | 28.6 s | $0.137 |
   | it_asset_management | 33.4 s | $0.159 |
   | training_background_checks | 29.3 s | $0.169 |
   | data_in_transit | 59.5 s | $0.297 |
   | network_auth | 36.7 s | $0.198 |
   | **five criteria** | **187.5 s sequential, ~60 s at pool 5** | **$0.96** |

   Either number is past a browser, proxy or MCP client timeout, and $0.96 a
   run is why `POST /analyses` refuses duplicate submissions. It returns
   `202` with an id in under a second; the client polls `GET /analyses/{id}`
   or subscribes to `GET /analyses/{id}/events`. The id is the state handle
   — the server keeps nothing per client.
2. **Upload mints an id; analysis and chat are bound to it.** Every
   `POST /documents` writes a new row and returns a unique `document_id`
   (SQLite integer). The API stores the file as
   `RAW_DIR/<uuid>-<sanitized-name>.pdf` so two uploads of the same bytes are
   still two documents — `ingest_file` keys uniqueness on path, and a unique
   path is how we get a unique id. **The name is sanitized, not trusted**: an
   upload's `filename` is client-controlled, and `../../../.env` escapes
   `RAW_DIR` whatever prefix precedes it (see [Documents](#documents)).
   `POST /analyses` and `POST /chat` require that `document_id`; there is no
   implicit "current document". A one-shot
   `POST /analyses` with a file is the UI convenience: ingest, then queue
   against the new id. Isolation is a library invariant, not an API extra:
   `retrieve()`, `chat()`, and `analyze_document()` already take
   `document_id`, and the API never passes `ALL_DOCUMENTS`.
3. **Chat streams over SSE, and only chat.** It is the one interaction
   where latency is felt token by token. Analysis progress uses SSE too but
   only as an *optional* upgrade over polling; polling is the contract.
4. **One thread pool, bounded, owned by the app.**
   `ThreadPoolExecutor(api_workers=2)` on `app.state`; each job opens its own
   `get_db(same_thread=False)` connection and closes it. SQLite serialises
   writes; two concurrent analyses is the honest ceiling for a single-file
   store, and the `queued` state is what a third request sees. No Celery, no
   Redis: a local demo does not need a broker, and the runner is one function
   behind an interface that a broker could implement later.
5. **One connection per thread, and the trace id carried across every
   `submit`.** Two things do not cross a `ThreadPoolExecutor` boundary, and
   both are load-bearing here.
   *Connections*: `db.py:44` states the invariant out loud — "concurrent use
   of one connection from two threads is still a bug; this flag only stops
   sqlite3 from catching a bug we do not have." So the inner pool of criterion
   runs does **not** share the worker's connection; each task opens its own
   `get_db(same_thread=False)` and closes it in a `finally`. Sharing happens
   to work on a serialized SQLite build, but it serialises every read, so it
   buys nothing and costs the invariant.
   *`contextvars`*: `trace_id` is a `ContextVar` (`logger.py:46`) that
   `_ContextFilter` reads per record, and `executor.submit` does not copy the
   context. Every `submit` — the job pool and the criterion pool both — goes
   through `contextvars.copy_context().run(...)`, or re-enters
   `trace_context(trace_id)` as its first statement. Without this the
   `analysis.criterion`, `agent.call` and `agent.tool` lines carry
   `trace_id: null` and the trace acceptance criterion is unmeetable.
6. **The report on disk is the report over the wire.** `ComplianceResult`
   already serialises; `AnalysisReport` (the five results plus totals) is
   persisted as JSON in the `runs` row the metrics store writes. `GET
   /analyses/{id}` reads it back — there is no second schema.
7. **Trace id in, trace id out.** Every request runs under
   `trace_context()`. An incoming `X-Trace-Id` is honoured (the MCP server
   and the UI send one); the response carries `X-Trace-Id` either way, and a
   job keeps the trace id of the request that started it, so one Claude
   Desktop tool call, its API request, the five criterion runs and their
   tool calls share one id in `.run/app.jsonl`.
8. **API key, static, optional.** `X-API-Key` checked against `API_KEY` when
   set; unset means open, which is the local demo. The OpenAPI spec declares
   the scheme so the connector story is complete; production would be
   OAuth 2.1 with per-tenant `document_id` scoping, and the docs say so.
9. **No framework beyond FastAPI + uvicorn.** Both are already implied by
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

`POST /documents` saves under `RAW_DIR/<uuid>-<sanitized-name>.pdf`, then
calls `ingest_file(path, conn, embedder, settings)`. The original filename is
what `GET /documents` returns; the uuid is only on disk so two sessions
cannot collide. Three details that are not optional:

**The filename is sanitized before it becomes a path.** `UploadFile.filename`
is whatever the client sent. Take `Path(name).name` to drop any directory
part, replace anything outside `[A-Za-z0-9._-]`, truncate to 100 characters,
fall back to `upload.pdf` if nothing survives, and then assert
`path.resolve().is_relative_to(RAW_DIR.resolve())` before opening the file for
write. The uuid prefix does not make this safe on its own:
`<uuid>-../../x.pdf` still resolves upward.

**The size cap is enforced while streaming, not after.** `api_max_upload_mb`
is checked chunk by chunk as the body is written to disk — `await
file.read()` on an endpoint that is open by default (`api_key` unset) is a
one-line OOM. Over the cap: delete the partial file, then `413`.

**`ingest_file` does not raise, so the exception handler never sees an ingest
failure.** Its contract is explicit (`ingest/pipeline.py:190`): "returning
what happened rather than raising. Only `ModelMismatch` escapes." A missing
`OPENAI_API_KEY` comes back as `IngestResult(status="failed",
error="EmbedderUnavailable: ...")`, so a handler that only maps exceptions
would answer `201` with zero chunks. `POST /documents` therefore branches on
`result.status`: `ingested`/`replaced` → `201`; `failed` → map the leading
exception name in `result.error` (`EmbedderUnavailable` → `503
embedder_unavailable`, `FileNotFoundError` → `422`, anything else → `502
ingest_failed`, carrying `result.error` as the message). `skipped` cannot
occur here — the path is always new.

Non-PDF → `415`; empty → `422`; `ModelMismatch` propagates and the handler
maps it to `409`. Unknown `{id}` → `404`.

`DELETE /documents/{id}` is one statement — `DELETE FROM documents WHERE id
= ?` — because `chunks` cascades and the `chunks_ad` trigger takes the FTS
and vec rows with it. That a cascade fires triggers is *not* guaranteed by
SQLite (the docs make it depend on `recursive_triggers`); it was verified on
SQLite 3.51, and the test below asserts it rather than trusting it.

### Analyses (jobs)

| Method | Path | Returns |
|---|---|---|
| `POST` | `/analyses` (JSON `{document_id, criteria?: [id]}` **or** multipart `file`) | `202 AnalysisStatus` — `{analysis_id, document_id, status: queued, created_at}`; missing/unknown `document_id` → `422` / `404`; no answer key → `503` *before* queueing; a duplicate submission → `200` with the in-flight analysis; multipart file = ingest then queue on the new id |
| `GET` | `/analyses/{id}` | `AnalysisStatus` — `{analysis_id, document_id, status, stage, progress: {done, total}, criteria: [{id, status, state?, confidence?}], report?: AnalysisReport, error?, trace_id, started_at, completed_at, totals: {latency_s, cost_usd, tokens}}` |
| `GET` | `/analyses/{id}/events` | SSE: `status`, `criterion` (one per finished criterion, with its result), `tool_call` (from `on_event`), `done` / `error`; closes after `done` |
| `GET` | `/analyses` | `AnalysisStatus[]` without `report`, newest first; `?document_id=` required to keep the list scoped to one contract |
| `POST` | `/analyses/{id}/cancel` | `202`; sets a flag the runner checks between criteria — a running criterion finishes, the rest are skipped |

Status machine: `queued → running → done | failed | cancelled`. `stage`
is human text (`ingesting`, `criterion 3/5: data_in_transit`, `evaluating`).
`report` is present only on `done` (and, partial, on `cancelled`).
`?detail=summary` on `GET /analyses/{id}` omits quotes and rationale — the
MCP default, because a full report is a lot of context.

**The key is checked before the job is queued.** `get_client()` raises
`AnswerUnavailable` before any request when `ANTHROPIC_API_KEY` is unset
(`generation/client.py:47`) — but on the worker thread that becomes a job
which fails 200 ms after a `202`, which is a worse answer than an error. The
handler calls `get_client(settings)` as a pre-flight and returns `503
no_api_key` if it raises; the client it built is the one the job then uses.

**Duplicate submissions do not run twice.** At $0.96 a run, a double-clicked
button is $2. A `POST /analyses` whose `(document_id, sorted(criteria))`
matches an analysis already `queued` or `running` returns `200` with *that*
analysis instead of `202` with a new one; an `Idempotency-Key` header
overrides the match when a caller genuinely wants a second opinion. Together
with the pool size, this is the answer to "what stops an open endpoint from
spending money" (see [Open questions](#open-questions)).

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

Pydantic, and where a library type exists it is reused, not mirrored.
`ComplianceResult` is already a pydantic model (`compliance/schemas.py:74`)
and goes over the wire as is. `Criterion` is a dataclass, which pydantic
accepts as a response model unchanged. `AnswerResult` is a dataclass holding
live objects — `Evidence`, `ToolCall`, `Usage` — so it is the one type that
*is* projected: a small `Answer` model carrying `text`, `citations` resolved
to `{evidence_id, quote, title, page_display, chunk_id, start, end}`, `usage`,
`cost_usd`, `stop_reason`, `ended_by`. New: `Document`, `AnalysisStatus`,
`AnalysisReport` (`analysis_id, document_id, filename, results:
ComplianceResult[], totals, cross_criterion_notes, created_at,
completed_at`), `ChatRequest`, `Error`.

Every error is `{error: {code, message, hint?}}` with a stable `code`
(`document_not_found`, `analysis_running`, `no_api_key`,
`embedder_unavailable`, `ingest_failed`, `payload_too_large`,
`unsupported_media_type`, `validation`). `hint` is the sentence a model can
act on ("call GET /documents to list document_id and filename").

One exception handler maps `AnswerUnavailable` / `EmbedderUnavailable` →
`503`, `HttpFailure` → `502`, `ModelMismatch` → `409`, `KeyError` on ids →
`404`, pydantic → `422`. It covers the paths that *raise*; the upload path
does not go through it, because `ingest_file` returns its failure instead of
raising, and `POST /documents` produces the same envelope from
`IngestResult.status` (see [Documents](#documents)). Both routes must emit
the same `code` for the same cause, and the tests assert that for
`embedder_unavailable`.

## Module layout

```
src/contract_analyzer/api/
  __init__.py
  main.py        create_app(settings) -> FastAPI; lifespan opens the pool, the
                 embedder, the metrics store and the shared HTTP client, and
                 closes them; app = create_app() for uvicorn
  deps.py        get_settings, get_embedder (one, on app.state), get_conn
                 (per-request, closed after -- never used by a streaming
                 generator, see Streaming), require_key, trace middleware
  schemas.py     response/request models, Error
  routes/
    health.py  documents.py  analyses.py  chat.py  metrics.py
  jobs.py        JobRunner: submit(document_id, criteria, trace_id) -> id;
                 status(id); cancel(id); the worker function; the fan-out
                 (per-subscriber queues plus the replay buffer)
  sse.py         event helpers: sse(event, data), the keepalive comment,
                 subscribe(job) -> Iterator[Event]
```

`JobRunner` is a class with an in-memory `dict[id, JobState]` **and** the
`runs` row: the dict is the live view (stage, progress, cancel flag,
subscribers), the row is the durable one. On startup, rows still `running`
from a previous process are marked `failed: interrupted` — a restart must not
show a job that will never finish.

The lifespan also warms and closes the shared HTTP client. `get_http_client`
lazily initialises a process-wide singleton with no lock
(`http_client.py:221`), so two threads racing on the first request can build
two; building it once at startup removes the race, and closing it at shutdown
is what stops uvicorn's reload from leaking sockets.

### The worker

```python
def _run(job):                        # submitted via copy_context().run
    with trace_context(job.trace_id), span("api.analysis", analysis_id=job.id):
        conn = get_db(settings, same_thread=False)
        try:
            analyze_document(job.document_id, conn, embedder, settings, client,
                             criteria=job.criteria, on_event=job.publish,
                             cancelled=job.cancelled,
                             workers=settings.analysis_workers)
        finally:
            conn.close()
```

`analyze_document()` is the prerequisite library function (five
`analyze_criterion` calls in their own inner pool of `analysis_workers`, then
the evaluator's cross-criterion pass, then `MetricsStore.end_run`).
`on_event` is the same callback the CLI prints; the API fans it out to the
SSE subscribers and the progress dict.

Concurrency inside the worker: the inner pool of `analysis_workers` criterion
runs **does not share the worker's connection**. Each task opens its own
`get_db(same_thread=False)` and closes it in a `finally`, for the reason in
decision 5, and is submitted through `contextvars.copy_context().run` so the
trace id survives the hop. The worker's own connection is for the run row and
the spans, written after the pool joins.

`analyze_document` also **tags every event with its criterion before passing
it on**. What the agent emits carries no criterion id — `agent.py:224` emits
`{"type": "tool_call", "surface", "name", "args", "returned", "new", "ids",
"error"}`, and only the `result` event names one (`analysis.py:169`) — so five
parallel runs would produce an unattributable interleaving that no UI can
render. The runner wraps the caller's callback per criterion,
`lambda e: on_event({**e, "criterion": c.id})`. `job.publish` is therefore
called from `analysis_workers` threads at once and must be thread-safe; see
Streaming.

## Streaming

Both streams are `sse-starlette` `EventSourceResponse`. A comment frame every
`api_keepalive_seconds` keeps proxies from closing an idle analysis stream.
The chat stream ends with `done` carrying `{usage, cost_usd, stop_reason,
ended_by}`; the client-side transcript appends `text` deltas and renders the
`citations` event once. `HttpFailure` mid-stream becomes an `error` event and
a clean close, never a broken connection.

**Fan-out, not one queue.** `queue.Queue` is single-consumer: with one queue
per job, a UI reconnect steals events from the first stream and two watchers
each see half. `JobState` therefore holds

* `subscribers: list[Queue]` behind a lock. `publish()` writes to each with
  `put_nowait` and drops the oldest item on `Full`, so a stalled reader can
  never block a criterion thread;
* `replay: deque(maxlen=api_event_buffer)` of everything published so far,
  plus the terminal event once one exists.

`subscribe(job)` takes the lock, copies `replay` into a fresh bounded queue,
appends that queue to `subscribers`, and releases. A client connecting at
second 40 therefore gets the criteria it missed and then the live ones, and a
client connecting *after* `done` gets the replay and the terminal event and a
closed stream rather than hanging until the keepalive gives up. The generator
removes its own queue in a `finally`.

**A streaming response owns its connection.** `/chat` does **not** take the
per-request `get_conn` dependency. Whether a dependency's teardown runs before
or after a streaming body is consumed has changed across FastAPI releases, and
`db.py:45` already names the fragile version of this contract ("the `/chat`
generator joins its worker before the dependency closes the connection").
Instead the generator opens `get_db(same_thread=False)` as its first act and
closes it in a `finally`: the connection's lifetime is the stream's lifetime
by construction, and nothing depends on framework ordering.

## Settings (`config.py`)

| Setting | Default | |
|---|---|---|
| `api_key` | unset | `X-API-Key`; unset = open |
| `api_workers` | 2 | analysis jobs in flight |
| `analysis_workers` | 5 | criterion runs inside one job. Also the rate-limit lever — `api_workers × analysis_workers` is the concurrent-request ceiling against the Anthropic API — and the tests set it to 1 (see below) |
| `api_max_upload_mb` | 25 | `413`, enforced while streaming to disk |
| `api_cors_origins` | `[]` | the UI is a different origin only outside Docker |
| `api_keepalive_seconds` | 15 | SSE comment cadence |
| `api_event_buffer` | 256 | per-subscriber queue depth and replay length; oldest dropped on overflow |

`CA_API_URL` is the *clients'* setting (UI, MCP), already in compose.

## Docker

`Dockerfile` runtime stage gains `fastapi`, `uvicorn[standard]`,
`sse-starlette`, `python-multipart` (a new `[api]` extra in `pyproject`); the
`dev` extra gains `httpx`, which `TestClient` needs and which is a different
package from the `httpx2` the application calls out on. The `api` service
gets `restart: unless-stopped` and the comment about the missing module goes.
The entrypoint already runs `uvicorn contract_analyzer.api.main:app`, so
`create_app()` must be bound to `app` at import time. The healthcheck already
targets `/health`.

## Trace propagation, end to end

```
UI / MCP ── X-Trace-Id ──► middleware: trace_context(id or new) ──► handler
                                   │
                                   └─► JobRunner.submit(trace_id)
                                            │  copy_context().run
                                            └─► worker: trace_context(trace_id)
                                                     │  copy_context().run  ×5
                                                     └─► analyze_criterion → agent.call
                                                                           → agent.tool
response ◄── X-Trace-Id ──┘
```

Both `copy_context()` hops are the load-bearing part: a `ContextVar` does not
cross `executor.submit` on its own, and the criterion pool is where most of
the log lines are made. See decision 5.

The MCP server mints one id per tool call and sends it; the UI mints one per
upload and shows it beside the run, which is the demo's "here is the same
id in the log" moment.

## Prerequisites

Library functions the API calls that do not exist yet, in the order they
are needed:

1. `compliance/report.py`: `analyze_document(document_id, conn, embedder,
   settings, client, *, criteria=None, on_event=None, cancelled=None,
   workers=None) -> AnalysisReport` — the runner (overall plan step 20). It
   owns the per-criterion connection, the `copy_context()` submit and the
   criterion tag on every event (see [The worker](#the-worker)). **Blocks
   `POST /analyses`.**
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
| 12c | `test: the runner -- parallel criteria, cancel, partial report` | scripted API, five criteria at `analysis_workers=1`; one parallelism test on a repeated outcome; assert every event carries its `criterion` and every criterion line its `trace_id` |
| 12d | `feat(api): app, health, criteria, documents` | `main.py`, `deps.py`, `schemas.py`, `routes/{health,documents}.py`; sanitized upload path, streamed size cap, `IngestResult.status` mapping; `[api]` extra |
| 12e | `feat(api): analysis jobs -- submit, poll, events, cancel` | `jobs.py`, `sse.py`, `routes/analyses.py`; key pre-flight, duplicate-submit guard, subscriber fan-out |
| 12f | `feat(api): streamed cited chat` | `routes/chat.py` |
| 12g | `feat(api): metrics endpoints over the store` | `routes/metrics.py` (after the store lands) |
| 12h | `test: api -- upload, jobs, sse, chat, errors, trace ids` | `TestClient`, fake embedder, scripted model API at `analysis_workers=1`; traversal and oversize uploads; two subscribers and a late one |
| 12i | `chore(docker): api service live` | `Dockerfile`, compose `restart`, extra |
| 12j | `docs(api): endpoints, jobs, streaming, the connector spec` | `docs/api.md`; `architecture.md` rows; export `openapi.json` to `docs/` as the §3.3 artefact |

Estimate: 12a–12c 2 h, 12d–12f 2.5 h, 12g 0.5 h, 12h 1.5 h, 12i–12j 1 h —
**7.5 h for step 12 alone**, against `00_overall_plan.md`'s "Phase 4 — API
(~1h)" and a total budget of 8–12 h. That line did not include the runner;
this one does. With the evaluator's 4.5 h on top, KPI, UI, MCP and docs are
unfunded, so the cut is decided **now, not at hour 10**:

**Cut first, by default: `/events` SSE for analyses.** Polling is already the
contract (decision 3), the UI's 2 s poll is what the demo shows, and cutting
it deletes the whole fan-out in [Streaming](#streaming) — about an hour, and
the hardest thing in this plan. The criterion tag on events stays either way:
the CLI's `on_event` printer needs it as much as a stream does, and it is one
lambda. `/chat` keeps its stream: that is the one interaction where
latency is felt token by token. **Then:** `DELETE /documents`, then cancel.

## Tests (`tests/test_api.py`)

`fastapi.testclient.TestClient` over `create_app(settings)` with
`EMBEDDING_PROVIDER=fake`, `ANALYSIS_WORKERS=1`, a temp database, and the
model reached through the scripted SSE transport from `conftest.py` — no
network, no key. Two things about the harness:

**`analysis_workers=1` in tests, or the suite is flaky.** `ScriptedAPI` is a
strict FIFO — `self.outcomes.pop(0)` next to `self.requests.append`, no lock
(`conftest.py:244`). Five criteria in parallel pop responses in
nondeterministic order, so no script survives. Pool size 1 restores the
order; the one test that must exercise real parallelism scripts a single
repeated outcome, where order does not matter. If a later test needs both,
make `ScriptedAPI` content-addressed — dispatch on the criterion named in the
system prompt — and put a lock around it.

**`TestClient` pulls `httpx`, which is not `httpx2`.** That is not a breach of
the one-HTTP-stack rule, which is about outbound calls from application code,
and the two packages have different module names so they coexist. If it
should be avoided anyway, `httpx2` ships `ASGITransport`, so
`httpx2.AsyncClient(transport=ASGITransport(app))` tests the app on the
project's own stack — at the cost of `pytest-asyncio` and starting the
lifespan by hand. `TestClient` is the choice; the alternative is recorded so
it is not rediscovered.

* `GET /health` reports `key_present: false` without a key; `/criteria`
  lists five with sub-requirements.
* upload the sample → `201` with `document_id` and `filename`; upload the
  same bytes again → `201` with a *different* `document_id`; `GET
  /documents` returns both `{document_id, filename}`; a `.txt` → `415`;
  oversize → `413` **and no partial file left in `RAW_DIR`**.
* `filename="../../../.env"` and `filename="..%2f..%2fx.pdf"` are stored
  inside `RAW_DIR` under a sanitized name, nothing is written outside it, and
  `GET /documents` still shows the name the client sent.
* `EMBEDDING_PROVIDER=openai` with no `OPENAI_API_KEY` → upload returns `503
  embedder_unavailable`, not `201` with zero chunks (`ingest_file` returns
  `status="failed"`; it does not raise).
* `DELETE /documents/{id}` leaves no rows in `chunks`, `chunks_fts` or
  `chunks_vec` — the cascade-fires-triggers assumption asserted rather than
  trusted — and `409` while an analysis on that document is running.
* `POST /analyses` without `document_id` → `422`; unknown id → `404` with
  hint. With a valid id the job runs on the pool with a scripted model;
  `GET` shows `running` with progress then `done` with a report whose five
  results validate as `ComplianceResult`; `?detail=summary` has no quotes.
* a second `POST /analyses` for the same `document_id` while the first is
  `running` → `200` with the *same* `analysis_id`, and one job on the pool;
  the same request with `Idempotency-Key` → `202` and a second job.
* two documents ingested; analysis and chat on A never return chunks from B
  (retrieval `document_id` filter).
* `/events` yields `status`, five `criterion`, `done`, then closes; every
  `tool_call` event carries the `criterion` it came from.
* two clients subscribed to one job each receive all five `criterion` events;
  a client subscribing *after* `done` receives the replay and the terminal
  event and the stream closes, rather than hanging.
* cancel after criterion 1 → `cancelled` with a one-result partial report.
* a third submission while two run → `queued`; runs after one finishes.
* chat `stream=true` → `text` deltas concatenate, one `citations` event,
  `done` with usage; `stream=false` → the same as one JSON; unknown
  `document_id` → `404` with `hint`.
* no key → `503 no_api_key` on chat and on `POST /analyses`, the latter
  *before* a job is queued so `GET /analyses` stays empty; `201` on upload
  (ingest needs no answer key).
* `X-Trace-Id` in → same id on the response and on every log line of the
  job, **including the `analysis.criterion`, `agent.call` and `agent.tool`
  lines emitted on the inner pool's threads** — the `contextvars` hop; none
  in → one minted and returned.
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
      analysis in `.run/app.jsonl`, including the tool-call spans emitted on
      the inner pool's threads: `jq 'select(.trace_id == null)'` over the
      run's lines is empty.
- [ ] `/chat` streams a cited answer for the MFA question with `p.4`.
- [ ] `docs/openapi.json` exported and linked from `architecture.md` as the
      §3.3 connector specification.
- [ ] An upload whose `filename` is `../../../.env` writes nothing outside
      `RAW_DIR`, and an upload with no embedding key returns `503`, not
      `201` with zero chunks.
- [ ] No handler contains logic the CLI does not have.

## Open questions

1. **`/v1` now or later?** Recommendation: later; one caller (MCP) to update.
2. **Should `POST /analyses` accept a file directly?** Recommendation: yes
   as a convenience — the UI's single "upload and analyse" button — but the
   two-step path is the documented one.
3. **Persist job state only in the metrics `runs` row, or also a `jobs`
   table?** Recommendation: the `runs` row plus the in-memory dict; a
   separate table is a second source of truth for the same fact.
4. **Rate limiting?** Not for a local demo: `api_workers ×
   analysis_workers` (2 × 5 = 10 concurrent requests to the Anthropic API) is
   the ceiling, and the duplicate-submit guard closes the obvious way to
   spend money by accident. Document both as the production gap alongside
   auth.
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

## What changed in this revision

Ten fixes from the 2026-08-24 review, in the order they appear above.

1. Decision 1 carries the measured latency and cost table instead of an
   estimate — `.run/analyze_sample.jsonl`, not a guess.
2. Upload filenames are sanitized and the write is confined to `RAW_DIR`;
   `<uuid>-<client filename>` was a path traversal.
3. `POST /documents` branches on `IngestResult.status`. `EmbedderUnavailable`
   never reaches an exception handler, because `ingest_file` does not raise.
4. The upload size cap is enforced while streaming to disk.
5. The criterion pool gets one connection per thread, restoring the invariant
   `db.py:44` states.
6. Every `submit` carries the trace context across the thread boundary;
   without it the trace-id acceptance criterion cannot be met.
7. `analyze_document` tags every event with its criterion, and `publish` is
   thread-safe — five parallel runs were otherwise unattributable.
8. SSE fan-out: per-subscriber bounded queues, a replay buffer, and defined
   behaviour for a late subscriber.
9. `/chat` opens its own connection inside the generator rather than taking a
   request-scoped dependency whose teardown order is framework-version
   dependent.
10. `POST /analyses` checks the key before queueing, and refuses duplicate
    submissions.

Also: `analysis_workers` (the rate-limit lever, and the fix for the
`ScriptedAPI` FIFO race in tests) and `api_event_buffer` added to settings;
the shared HTTP client warmed and closed in the lifespan; the
cascade-fires-triggers assumption asserted rather than trusted; and the
schedule reconciled with the overall plan's budget, with the first cut
decided in advance.
