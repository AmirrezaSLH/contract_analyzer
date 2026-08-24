# The HTTP API

One backend, four consumers. The React UI, the MCP server, an external
connector and the CLIs all reach the same functions, and this is the HTTP
surface over them.

**The rule that shapes everything here: the API contains no logic the CLI does
not have.** Every handler is `parse the request -> call a library function ->
shape the response`. `POST /analyses` and `scripts/analyze.py` call the same
`analyze_document()` with the same arguments; the API adds a job id and an HTTP
status code, and nothing else. When a handler starts wanting to decide
something, that decision belongs a layer down where the command line can reach
it too.

## Running it

```bash
pip install -e ".[api]"
uvicorn contract_analyzer.api.main:app --port 8100
# or
make docker-up            # then http://localhost:8100/docs
```

Neither key is required to start. Without `ANTHROPIC_API_KEY` you can still
upload, list, read outlines and browse the spec; analysis and chat answer `503
no_api_key` and say which `.env` key is missing. `GET /health` reports
`key_present` so a UI can grey out the buttons rather than discovering it three
clicks later.

## The shape of it

| | |
|---|---|
| **Upload mints an id** | Every `POST /documents` returns a new `document_id`, even for identical bytes. Analysis and chat both require one; there is no implicit "current document". |
| **Analyses are jobs** | `POST /analyses` answers in milliseconds with an `analysis_id`; the run takes 60–180 s. Poll `GET /analyses/{id}`, or subscribe to its `/events`. |
| **Chat streams** | It is the one interaction where latency is felt token by token. `stream=false` returns the same answer as one JSON body. |
| **The server is stateless** | The ids are the state. No sessions, no server-side transcript: the client owns its history and passes it back. Chat's three controls are per *question* for the same reason. |
| **One trace id, end to end** | `X-Trace-Id` in is honoured and returned; the job keeps it, and so does every criterion, tool call and HTTP retry underneath. |

### Endpoints

| Method | Path | |
|---|---|---|
| `GET` | `/health` | Liveness, configuration, counts. Open even when a key is required. |
| `GET` | `/criteria` | The five criteria with their sub-requirements. Also open. |
| `POST` | `/documents` | Multipart upload. Parses, chunks, embeds, returns `document_id`. |
| `GET` | `/documents` | Everything stored, newest first, each with its last analysis. |
| `GET` | `/documents/{id}` | One document, plus the analyses run against it. |
| `GET` | `/documents/{id}/sections` | The outline, for a section picker. |
| `DELETE` | `/documents/{id}` | The document, its chunks, its vectors, its file. Its **analyses are kept**. `409` while an analysis of it is queued or running. |
| `POST` | `/analyses` | Queue a run. `202`, or `200` with the analysis already doing this. |
| `GET` | `/analyses` | One document's analyses (`?document_id=` required). |
| `GET` | `/analyses/{id}` | Status, progress, and the report once there is one. Answered from the stored record after a restart. `?detail=summary` drops quotes and rationale. |
| `GET` | `/analyses/{id}/events` | SSE: `status`, `criterion`, `tool_call`, `correction`, then `done` or `error`. `409` for an analysis this process is not running. |
| `POST` | `/analyses/{id}/cancel` | Skip what has not started. `409` for an analysis this process is not running. |
| `POST` | `/chat` | A cited answer over one contract, streamed or not. Model, retrieval mode and passage count are per-question. |
| `GET` | `/metrics/*` | Declared; `503` until the metrics store lands. |

## Decisions worth defending

### Jobs, not long requests

Measured on the sample contract: 28–60 s per criterion, **187 s sequential and
about 60 s at five workers, for $0.96**. Either number is past a browser's
timeout, a proxy's, and every MCP client's. So `POST /analyses` returns an id
and the client comes back. The id is the whole of the server's per-client
state, which is what lets the UI start a job and an MCP tool watch it.

Polling is the contract; `/events` is an upgrade. If the SSE stream has to be
cut for time, the UI's two-second poll still works.

### Upload mints an id, and the bytes are never trusted

`POST /documents` writes to `RAW_DIR/<uuid>-<sanitized name>.pdf`. The uuid is
what makes two uploads of the same contract two documents: `ingest_file` keys
uniqueness on path, so a fresh path is a fresh id, and two people demoing at
once cannot see each other's analyses.

The sanitisation is not decoration. `filename` is whatever the client put in
the multipart header, and `../../../.env` is a path — a uuid prefix does not
fix it, because `<uuid>-../../x.pdf` still resolves upward. So the name is
NFKC-normalised (a fullwidth solidus should be *seen* as a separator, not
quietly turned into an underscore), reduced to a basename, stripped to
`[A-Za-z0-9._-]`, truncated, and the assembled path is checked to be inside
`RAW_DIR` before anything is opened for writing. The original name is kept in
the database, where it is data rather than a path, and it is what `GET
/documents` shows.

The size cap is enforced **while the body streams to disk**, chunk by chunk,
and a rejected upload deletes its partial file. `await file.read()` on an
endpoint that is open by default is a one-line way to run the container out of
memory.

### `ingest_file` reports rather than raises

This one is worth knowing before reading the upload handler. `ingest_file`'s
contract is *"returning what happened rather than raising. Only `ModelMismatch`
escapes."* A missing `OPENAI_API_KEY` comes back as
`IngestResult(status="failed", error="EmbedderUnavailable: ...")`. A handler
that only mapped exceptions would answer `201` with a document of zero chunks
— which then answers every question with "no relevant passages". So the route
branches on `result.status`, and `errors.from_ingest_error` builds the same
envelope, with the same `code`, that the exception path would have produced.
The test asserts both routes agree on `embedder_unavailable`.

### A list row is drawn from one query, not one per row

`GET /documents` carries `pages`, `chunks`, `ingested_at` and `last_analysis`
because that is one table row on a screen, and the alternative is a client
calling `GET /documents/{id}` once per document — an N+1 on every re-render of
a UI that re-renders on every click.

`last_analysis` is `null` when nothing has been run, and otherwise carries
`{analysis_id, status, completed_at, states, needs_review}`. `states` is a
**count per compliance state**, not a summary sentence: one client writes "5 of
5 compliant", the next writes "2 gaps found", and neither wording belongs in
the API. It is counted in SQL — `json_each` over the stored report — so
producing five integers does not cost ~30 KB of JSON parsing per document.

The pick of *which* analysis is the newest tie-breaks on `rowid`, not on
`analysis_id`. `created_at` has one-second resolution and the id is random hex,
so an id tie-break is stable but not chronological; a row labelled "the last
analysis" has to be the last one.

### The three chat controls are the caller's, and `model` is an allowlist

`POST /chat` takes `model`, `retrieval_mode` and `top_k`. All three are
optional and all three move *this question's* defaults, leaving the process
alone — there is no session for them to belong to. `retrieval_mode` and `top_k`
set what the search tool does when the model does not choose; the model can
still ask for `keyword` on an identifier, which is the reason those arguments
are exposed to it at all.

`model` is validated against `chat_models` and **not** against free text. This
endpoint is open when `API_KEY` is unset, so an unchecked model id is an
invitation to spend this deployment's key on whatever the caller names. The
list is published by `GET /health`, along with the retrieval defaults and the
upload cap, so a client renders exactly the choices that will be honoured
rather than a copy of them that drifts when `settings.json` changes.

The answer reports the model that **ran**, not the one that was asked for —
`Answer.model` and the `done` event both carry it. Those differ the moment a
default or a fallback intervenes, and a usage line that reports the request is
the wrong half of the story.

### One citation, one set of field names

A chat citation is `{evidence_id, text, section_ref, page_display, chunk_id,
verified, start, end}` — `ResolvedQuote`'s names, deliberately. The same fact
was `title`/`quote` on an answer and `section_ref`/`text` on a report, which
bought every client a translation layer whose only job was to paper over a
naming accident. `verified` is computed the same way on both, so the marker
means one thing wherever a quote is shown.

### Isolation is a library invariant, not an API feature

`retrieve()`, `chat()` and `analyze_document()` all take `document_id` as a
required keyword, and the vector index partitions on it, so a scoped search
returns that contract's true *k* nearest rather than whatever survives a global
top-k. The API's whole contribution is never passing `ALL_DOCUMENTS`. That is
why the isolation test lives here *and* in `test_retrieval.py`: the API cannot
break it by forgetting, only by trying.

### Two thread-crossings, both deliberate

SQLite connections and thread boundaries are the two places this API could
quietly corrupt itself, so both are written down:

* **`get_conn` opens with `same_thread=False`.** Starlette runs a sync
  dependency in a worker thread and an `async` endpoint on the event loop, so a
  connection is *created* on one thread and *used* on another. That is not
  concurrent use — each step finishes before the next begins — but sqlite3's
  default check cannot tell the difference and refuses it. Handing one
  connection to two threads *at once* is still a bug; the flag only stops
  sqlite3 catching a bug we do not have.
* **The upload runs `ingest_file` in a threadpool.** The handler has to be
  `async` for `await file.read()`, and parsing, chunking and embedding a
  contract is seconds of blocking work. Left inline it would stall every other
  request in flight.

**Streaming responses take neither.** `/chat` and `/analyses/{id}/events` open
their own connection inside the generator and close it there. Whether a
dependency's teardown runs before or after a streaming body is consumed has
changed across FastAPI releases, and a connection closed under a half-finished
chat is a miserable way to discover which version you are on. Opening it in the
generator makes the lifetime the stream's by construction.

### The event stream is a fan-out, not a queue

`queue.Queue` has one consumer. This one has several plausible ones: a UI that
reconnects, a second tab, an MCP tool watching a job the UI started. So each
subscriber gets its own bounded queue, written with `put_nowait` and
drop-oldest — **a browser tab nobody is reading must never hold up a criterion
thread** — plus a replay buffer, so a client joining at second 40 receives what
it missed and then the live events, and a recorded terminal event, so
subscribing *after* the job finished replays and closes rather than hanging
until a keepalive gives up.

One lock covers `publish` and `subscribe` together. That is what stops an event
slipping between the replay copy and the subscriber's registration, which is
how a fan-out normally loses or duplicates exactly one event.

### What is decided before work is accepted

Two failures are the caller's and get answered immediately rather than becoming
a job that dies in thirty seconds: an unknown `document_id` or criterion id,
and a missing answer key. `get_client()` raises before any request when
`ANTHROPIC_API_KEY` is unset; on the worker thread that would be a `202`
followed by an instant failure, which is a worse answer than an error.

A submission matching one already queued or running returns **that** analysis
with `200` instead of starting a second run. At roughly a dollar a run, a
double-clicked button is a real cost. `Idempotency-Key` overrides the match for
a caller that genuinely wants a second opinion.

### Two halves of one analysis, and which one wins

An analysis is a **row** and a **live job**, and they hold different things.

| | The `analyses` row | The `JobState` dict |
|---|---|---|
| Survives a restart | yes | no |
| Visible to another worker | yes | no |
| Carries the report | yes | yes, while the process lives |
| `stage`, live `progress` | no | yes |
| SSE stream, cancel flag | no | yes |

`GET /analyses/{id}`, `GET /analyses` and the `analyses` array on
`GET /documents/{id}` read **the dict first and the row second**: the dict is
the one being updated, and the row answers everything else — which, after a
restart, is everything. A row supplies the terminal values its status implies
rather than invented ones: `stage` is the status, `progress` is what the row
says finished, and the per-criterion list is rebuilt from the stored report.

The lifecycle is written by `analyze_document`, not by the API, so `make
analyze` fills the same table: a report produced on the command line comes back
out of `GET /analyses/{id}`. The one state HTTP has and the CLI does not —
accepted but not yet started — is the only row the job runner writes itself.

`DELETE /documents/{id}` keeps the analyses. `analyses.document_id` carries no
foreign key: the report is the deliverable, it is self-contained, and one that
disappears because somebody tidied up the corpus is not a record. Roughly
**30 KB per five-criterion report**, so a thousand analyses is ~30 MB; there is
no retention policy and none is planned until someone asks for one.

### Durable is not distributed

With `uvicorn --workers 2`, a second worker can **read** a neighbour's analysis
and its report. It cannot stream its events or cancel it, because `Broadcast`
and the cancel `threading.Event` are per-process objects — both operations
answer `409 not_live_here` with a hint to poll instead. For the same reason
`POST /analyses` de-duplicates against **this** process's live jobs only: it
cannot hand back a stream or a cancel handle it does not own, so a duplicate
submission against another worker's run starts a second run. Fixing either
means a broker, which is deliberately out of scope for a local demo.

A run whose process was killed leaves a row saying `running`. The next startup
reconciles it to **`interrupted`** — a sixth `JobStatus` — before serving
anything. Not `failed`: nothing refused, the machine went away, and the client
should be told to run it again.

### Cancellation, honestly

`cancelled()` is polled before each criterion starts, so cancel skips whatever
has not begun and the report lists those ids in `skipped` with
`status="cancelled"`. At `analysis_workers >= len(criteria)` all five start at
once and there is nothing left to skip — cancel then only stops a job still
waiting for a free worker. Stopping a *running* criterion would mean threading
the flag into the agent loop between tool calls, which belongs to
`generation/`, not to the API.

## Errors

Every failure is the same shape:

```json
{"error": {"code": "document_not_found",
           "message": "No document with id 42.",
           "hint": "Pick a contract from the library, or upload one -- every upload gets its own id."}}
```

`code` is stable and is the thing to branch on: a model reading an MCP tool
result can act on `document_not_found`, and cannot act on `404`. `hint` is the
sentence that says what to do next, which is the difference between a model
retrying blindly and a model retrying correctly.

**`hint` is written for a person.** It has two readers — a model recovering
from a failed tool call, and a reviewer reading the second line of an error
surface in the UI — and one sentence serves both as long as it names the
*action* rather than the endpoint. *"Pick a contract from the library, or
upload one"* tells the model everything a spelled-out route would, and tells
the reviewer something a route spelling does not.

| `code` | Status | |
|---|---|---|
| `unauthorized` | 401 | Missing or wrong `X-API-Key`. |
| `document_not_found`, `analysis_not_found` | 404 | With a hint naming the endpoint that lists them. |
| `analysis_running`, `not_running`, `model_mismatch` | 409 | The resource is busy, or already in the state asked for. |
| `payload_too_large` | 413 | Over `api_max_upload_mb`. |
| `unsupported_media_type` | 415 | Not a PDF. |
| `validation` | 422 | The body, or an unknown criterion id. |
| `no_api_key`, `answer_unavailable`, `embedder_unavailable`, `metrics_unavailable` | 503 | A key or a dependency is not configured. |
| `upstream_failure`, `ingest_failed` | 502 | An upstream call failed after its retries. |
| `internal` | 500 | Unhandled. The message is not echoed — an unhandled exception's text is as likely to be a file path as an explanation — but the `X-Trace-Id` is, and it is on every log line of that request. |

## Tracing

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

Both `copy_context()` hops are load-bearing: a `ContextVar` does not cross
`executor.submit` on its own, and the criterion pool is where most of the log
lines are made. A live run of one criterion produced 23 lines under one id,
across `api.analysis`, `analysis.document`, `analysis.criterion`, `agent.run`,
`agent.call`, `agent.tool` and `retrieve`. The only untraced line in the file
was `api.startup`, which happens outside any request.

## Authentication

`X-API-Key` compared against `API_KEY` in constant time. Unset means open,
which is the local demo; `/health` and `/criteria` stay open either way,
because a healthcheck that needs a credential fails for the wrong reason.

It is declared as an `APIKeyHeader` rather than a plain header so that it
appears in the OpenAPI document as a security scheme — that document is the
connector deliverable, and a spec that does not describe its own auth is not
one.

**This is not what production would use.** Production is OAuth 2.1 with
`document_id` scoped per tenant, so that a token can reach its own contracts
and no others; today isolation is enforced but ownership is not, and any holder
of the key can read any `document_id`. Rate limiting is likewise absent: the
pool size (`api_workers × analysis_workers` = 10 concurrent model requests) and
the duplicate-submit guard are the only limits, which is enough for a local
demo and is stated here rather than left to be assumed.

## Settings

| Setting | Default | |
|---|---|---|
| `API_KEY` (`.env`) | unset | `X-API-Key`; unset = open. A secret, so it is not in `settings.json`. |
| `chat_models` | the three current ids | The allowlist `POST /chat` validates `model` against, published by `/health`. |
| `api_workers` | 2 | Analyses in flight. SQLite serialises writes; a third submission is `queued`. |
| `analysis_workers` | 5 | Criteria in parallel inside one job. With `api_workers`, the concurrent-request ceiling. |
| `api_max_upload_mb` | 25 | `413` above, enforced while streaming. |
| `api_cors_origins` | `[]` | Empty on purpose: the UI is served from this process. |
| `api_keepalive_seconds` | 15 | SSE comment cadence. |
| `api_event_buffer` | 256 | Per-subscriber queue depth and replay length. |

## The OpenAPI document

`docs/openapi.json` is exported from the running app and is the connector
specification for assignment §3.3. Every operation has a summary and a
description, every 4xx and 5xx carries the `Error` schema, and the security
scheme is declared. Regenerate it after changing a route:

```bash
python scripts/export_openapi.py
```

## What is not here yet

* **`/metrics/*` returns 503.** The store (`spans`, `criterion_results`, and
  the query layer over them) is the next step; the `analyses` table it will
  join against already exists and is populated. The endpoints are declared now because
  the UI and the connector are written against the spec, and an endpoint that
  is documented and honestly unavailable is a better contract than one that
  appears later and changes the spec's shape.
* **Streaming and cancellation are per-process.** The record is durable; the
  stream and the cancel flag are not. See *Durable is not distributed* above.
* **`/v1` prefix.** One caller (MCP) would have to change; deferred until there
  is a second version to distinguish it from.
