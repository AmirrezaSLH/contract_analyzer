# Step 12 — the HTTP API: what was built

**2026-08-24.** Companion to `05_api_plan.md`. 14 commits, 352 tests green,
`make lint` clean. Written after the fact, so where it disagrees with the plan
the plan was wrong.

## What is running

`make api`, or `uvicorn contract_analyzer.api.main:app`. Verified live against
the real model, not only through `TestClient`:

* `GET /health` → `ok`, with the trace id echoed from the request header;
* upload → `document_id`, 21 pages, 102 chunks, `spine_source: headings`;
* `POST /chat` streamed a cited answer to the MFA question quoting **§6.2 at
  p.4**, §6.7(c) at p.5, the p.5 summary table row, §8.2 at p.6, and Exhibit G
  G3/G13 — which is the plan's acceptance criterion, met;
* `POST /analyses` with one criterion: `202` in milliseconds, streamed
  `status → tool_call ×2 → criterion → done`, then `Fully Compliant` at
  confidence 0.95, three verified quotes, **37.5 s and $0.134**;
* a second identical submission returned `200` with the same `analysis_id`;
* `?detail=summary` came back with quotes and rationale emptied;
* one `X-Trace-Id` from the request appeared on **all 23 log lines** of the
  job, across `api.analysis`, `analysis.document`, `analysis.criterion`,
  `agent.run`, `agent.call`, `agent.tool` and `retrieve`. The only untraced
  line in the whole file is `api.startup`, which is outside any request.

## Deviations from the plan, and why

**The runner is `contract_analyzer/report.py`, not `compliance/report.py`.**
Exporting `analyze_document` from `compliance/__init__` closed an import cycle:
importing `compliance` imported the runner, which imports
`generation.analysis`, which imports `compliance.criteria`, still mid-import.
The cycle was not a quirk to route around — it was the module saying which
layer it is on. The runner uses `compliance` for the criteria and the schema
and `generation` for the agent, and those two already refer to each other, so
it can live in neither. `documents.py` is at the root for the same reason.

**The catalogue is `documents.py`, not four functions in `db.py`.** `db.py` is
the connection factory and the schema guard; listing, outlining and deleting
documents is a different job with a different reason to change.

**Two modules the plan did not name.** `uploads.py` (sanitising, streaming to
disk, containment) and `errors.py` (the envelope and the exception mapping)
were going to be paragraphs inside `routes/documents.py` and `main.py`. Both
are security-relevant and independently testable, which is a poor fit for
prose inside a handler.

**`publish()` takes a dict, not `**kwargs`.** A `tool_call` event has a field
called `name` and so did the signature. `TypeError: got multiple values for
argument 'name'`, on the worker thread, failing the whole job. Found by the
tests, not by reading.

**Two thread-crossings the plan did not anticipate**, both of which broke the
first working version:

* `get_conn` must open with `same_thread=False`. Starlette runs a sync
  dependency in a worker thread and an `async` endpoint on the event loop, so
  the connection is created on one thread and used on another. `db.py:38` had
  already written this down for "the later API" — it was right.
* `ingest_file` must run through `run_in_threadpool`. The upload handler has to
  be `async` for `await file.read()`, and ingestion is seconds of blocking
  work that would otherwise stall every request in flight.

**The metrics endpoints are declared and answer 503** rather than being absent.
The OpenAPI document is a deliverable and the UI is written against it; an
endpoint that is documented and honestly unavailable is a better contract than
one that appears later and changes the spec's shape.

**`analysis_workers` was added as a setting**, as the plan's revision proposed:
the pool size, the outbound rate-limit lever (`api_workers × analysis_workers`
= 10 concurrent model requests), and `=1` in tests, because `ScriptedAPI` is a
FIFO with no lock.

## The ten review fixes, as built

| # | Fix | Where |
|---|---|---|
| 1 | Measured latency and cost, not an estimate | `docs/api.md`, and again live above |
| 2 | Upload filenames sanitized and contained | `api/uploads.py`; four hostile-name tests |
| 3 | `IngestResult.status` branched on | `routes/documents.py`, `errors.from_ingest_error` |
| 4 | Size cap enforced while streaming | `uploads.save_upload`, partial file deleted |
| 5 | One connection per criterion | `report.py`; the path is read on the calling thread |
| 6 | `copy_context()` on every submit | `report.py` and `api/jobs.py`; asserted from the JSON log |
| 7 | Events tagged with their criterion, delivered under a lock | `report._Emitter` |
| 8 | SSE fan-out with replay and a recorded terminal event | `api/sse.py` |
| 9 | Streaming responses own their connection | `routes/chat.py`, `deps.py` docstring |
| 10 | Key pre-flight and duplicate-submit guard | `routes/analyses.py`, `jobs.find_live` |

## Not done

* **`make docker-up` was not verified here.** `docker compose config` validates
  and the entrypoint's `uvicorn contract_analyzer.api.main:app` matches the
  module-level `app`, but this machine's user is not in the `docker` group, so
  the image was never built. Run `make docker-build && make docker-up` before
  relying on it.
* **`/metrics/*` is a stub.** The store is the next step.
* **The evaluator** has not landed; `ComplianceResult` carries no `evaluator`
  field yet and `cross_criterion_notes` is present and empty, so its arrival
  will not change the wire format.
* **Analyses do not survive a restart.** They live in a dict on the app; the
  404 hint says so. The metrics `runs` row is meant to become the durable half.
* **Cancel cannot stop a running criterion.** It skips what has not started.
  At `analysis_workers >= len(criteria)` all five start at once, so cancel only
  helps a job still waiting for a worker. Stopping a running one means
  threading the flag into the agent loop, which is `generation/`'s change.

## What the next step inherits

`analyze_document()` already returns everything a `runs` row needs — totals,
per-criterion states and confidences, the trace id, timestamps — and
`JobRunner` is deliberately a class over a dict rather than a bare dict, so
the store slots in as the durable half of the same interface. `GET
/analyses/{id}` reads the report back from memory today and from the row
tomorrow, with no change to the wire format.
