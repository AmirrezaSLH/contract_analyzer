# Step 13 — the metrics store: what a run cost, and whether it was any good

**Status: draft for review, 2026-08-24.** Supersedes steps 22–23 and 26 of
`00_overall_plan.md` where they conflict. Assumes step 12 (the API) is in,
which it is. Blocks the KPI dashboard; blocked by nothing.

**The one-sentence version.** Every span this system emits already carries the
numbers a KPI needs — latency, tokens, cost, model, mode, criterion, verdict —
and throws them at a JSON file nobody queries. This step gives them a table.

## Why now

Three loose ends, in the order they will embarrass us:

1. **`docs/openapi.json` is committed as the §3.3 connector specification and
   four of its seventeen operations answer `503`.** That is defensible for a
   week and not for a submission.
2. **Analyses die with the process.** `JobState` lives in a dict; `GET
   /analyses/{id}` returns a 404 whose hint apologises for it. The plan for
   fixing that was always the `runs` row.
3. **The KPI dashboard is an explicit assignment deliverable** and nothing else
   blocks it.

## The central decision

**The store is a sink for telemetry that already exists, not a second
instrumentation pass.**

Here is what a live run actually emits today, read out of `.run/app.jsonl`:

| span | attributes already on it |
|---|---|
| `agent.call` | `surface, turn, model, effort, stop_reason, structured, input_tokens, output_tokens, cost_usd, latency_ms, status` |
| `agent.run` | `surface, turns, tool_calls, evidence, evidence_tokens, ended_by, max_tool_calls, tokens, cost_usd` |
| `agent.tool` | `tool, mode, top_k, returned, new, retrieved, evidence_tokens` |
| `analysis.criterion` | `criterion, document_id, state, confidence, needs_review, structure_rounds, cost_usd` |
| `analysis.document` | `analysis_id, document_id, criteria, workers, parallel, status, cost_usd, mean_confidence, needs_review, skipped` |
| `api.analysis` | `analysis_id, document_id` |
| `chat` | `document_id, history, evidence, citations, tool_calls, grounded, cost_usd` |
| `retrieve` | `document_id, mode, top_k, results` |
| `ingest.{file,parse,chunk,embed,write}` | `path, chunks, pages, spine_source, model` |

Every one of them carries `trace_id`, `span_id`, `parent_span_id`,
`latency_ms` and `status`, because `logger.span()` puts them there.

So `spans` is written by a **`logging.Handler` on the project's root logger**,
which turns `span.end` records into rows. The consequences are the argument:

* **Eight modules change by zero lines.** `generation/`, `retrieval/`,
  `ingest/`, `compliance/` get no `record_span()` calls threaded through them,
  and no future module has to remember to add one.
* **The CLI is instrumented for free.** `scripts/analyze.py` produces the same
  spans as the API, because both call `configure_logging`. A KPI page that only
  sees API traffic is measuring the surface, not the system.
* **Chat and ingest are captured too**, not just analyses — so "retrieval
  health" and "what does an upload cost" are queryable without a second design.
* **The log file stays the source of truth for a walkthrough**, and the table
  becomes the source of truth for an aggregate. Neither replaces the other, and
  they cannot disagree, because they are the same records.

The rejected alternative is explicit `store.record_span()` calls at each site.
It is more direct to read at one call site and worse everywhere else: eight
modules to touch, a second thing to forget, and a `metrics` import in
`retrieval/` for no reason.

### What the handler must not do

Telemetry must never hold up an analysis. The same rule the SSE fan-out
follows applies here, for the same reason:

* the handler pushes onto a **bounded** `queue.Queue` and **drops on overflow**
  — it never blocks a criterion thread to record that a criterion thread was
  busy;
* one **daemon writer thread** drains it and writes in batches (SQLite
  serialises writes; a burst of ~70 rows per analysis is nothing, but 70
  separate transactions from five threads is silly);
* a drop increments a counter that `GET /metrics/summary` reports, because a
  metrics system that silently loses data is worse than one that says so;
* **`emit()` never raises.** `logging.Handler.handleError` swallows, but the
  queue put and the row build happen before that, so they are wrapped
  explicitly. A malformed span attribute must not fail the run it describes.

## Design decisions

1. **`runs` is written explicitly; `spans` is written by the handler.** A run
   is a business fact with a lifecycle — it exists as `queued` before anything
   is logged, and it has to be reconcilable after a crash. A span is a log
   line. Deriving runs from `analysis.document` spans was considered and
   rejected: the row must exist *before* the span ends, or a job that never
   finishes leaves no trace of having started.
2. **`analyze_document(..., store=None)` opens and closes the run.** Not
   `JobRunner`, because then `scripts/analyze.py` would produce no metrics and
   the API would contain logic the CLI does not have — the invariant step 12
   was built around. Both callers pass a store.
3. **The report JSON lives in the run row; the derived columns are an index
   over it.** `report_json` is the same `AnalysisReport` the CLI writes to
   disk, so there is still no second schema. `mean_confidence`,
   `quotes_verified` and the rest are columns because you cannot aggregate over
   a blob, not because they are a second truth.
4. **The metrics tables get their own file, `metrics.sql`, applied by the
   store.** `db.py` runs `SCHEMA_PATH.read_text().format(dim=dim)` — so every
   literal `{` or `}` in `schema.sql` is a format placeholder. A metrics schema
   full of `json_extract(attrs, '$.model')` would break it, and escaping every
   brace as `{{` is a trap laid for the next person. The metrics tables have
   nothing to do with the vector width; they do not belong in a file that is
   templated on it.
5. **Same database file, no foreign key to `documents`.** One file is the whole
   storage argument of this project, and joining a run to its document is worth
   having. But `DELETE /documents/{id}` must **not** take the KPI history with
   it — history that vanishes when you tidy up is not history. So
   `document_id` and `filename` are denormalised onto the run row and there is
   no `REFERENCES`.
6. **`run_id` becomes a `ContextVar`, beside `trace_id`.** Step 21 of the
   overall plan always said `trace_id/span_id/run_id`; only the first two were
   built. Without it, attributing a span to a run means guessing from
   `trace_id`, and one trace legitimately contains an upload *and* an analysis.
   `analyze_document` sets it; `_ContextFilter` stamps it; the handler reads
   it. Roughly fifteen lines in `logger.py`.
7. **Percentiles in SQL, not in Python.** SQLite 3.51 has window functions and
   this project ships 3.51 (verified). `p95` is
   `row_number() over (order by latency_s)` against `count(*) over ()`; no
   numpy, no pandas, no fetching every row to sort it in the API process.
8. **Every span is stored. No sampling.** At demo scale one analysis is ~70
   rows and a thousand analyses is a few megabytes. A sampling knob nobody will
   ever tune is a knob that will be set wrong during the demo.
9. **Evaluator columns exist now and are nullable.** `evaluator_verdict`,
   `evaluator_accepted/revised/fallback`, `quotes_claimed` — the same argument
   as `cross_criterion_notes` in the report: a field that is present and empty
   costs nothing, and its arrival must not be a migration in the middle of a
   demo week.

## Schema (`metrics.sql`)

Three tables. Column lists are indicative, not final; the shapes and the
reasons are the part to review.

### `runs` — one per `analyze_document` call

`run_id` (= `analysis_id`), `trace_id`, `document_id`, `filename`, `surface`
(`api` | `cli`), `status` (`queued` → `running` → `done` | `failed` |
`cancelled` | `interrupted`), `criteria_requested/completed/skipped`,
`latency_s`, `cost_usd`, `input_tokens`, `output_tokens`, `tool_calls`,
`needs_review`, `capped`, `mean_confidence`, `quotes_total`,
`quotes_verified`, `evaluator_accepted/revised/fallback` (nullable),
`error`, `report_json`, `created_at`, `started_at`, `completed_at`.

`quotes_total` / `quotes_verified` are the hallucination KPI and they are
counted here, from `ResolvedQuote.verified`, rather than recomputed by the
dashboard from the blob.

### `criterion_results` — one per criterion per run

`run_id`, `criterion_id` (primary key together), `state`, `confidence`,
`raw_confidence`, `needs_review`, `ended_by`, `structure_rounds`,
`tool_calls`, `cost_usd`, `input_tokens`, `output_tokens`, `quotes_total`,
`quotes_verified`, `latency_s`, `evaluator_verdict` (nullable), `created_at`.

This is what makes "state distribution *per criterion*" and "which criterion
is expensive" one query instead of five JSON extractions. It is also the drift
signal: the same document hash producing a different state for one criterion
across runs.

### `spans` — one per `span.end`

`span_id` (PK), `parent_span_id`, `trace_id`, `run_id`, `name`, `status`,
`latency_ms`, `ts`, plus the attributes worth having as columns because
everything queries them — `surface`, `criterion`, `document_id`, `model`,
`input_tokens`, `output_tokens`, `cost_usd` — and `attrs` as JSON for the
rest. `parent_span_id` is what makes `/metrics/runs/{id}/spans` a waterfall
rather than a list.

Indexes on `runs(created_at)`, `runs(document_id)`, `spans(run_id)`,
`spans(trace_id)`, `spans(name, ts)`.

## `MetricsStore` (`metrics/store.py`)

The API already assumes this surface; it was written against it.

| Method | |
|---|---|
| `start_run(analysis_id, document_id, filename, criteria, surface, trace_id)` | writes `queued`; returns the row |
| `mark_running(run_id)` | |
| `end_run(run_id, report)` | derives every column from the `AnalysisReport`, stores `report_json` |
| `fail_run(run_id, error)` | |
| `get_run(run_id)` / `runs(limit, document_id=None)` | the durable read, and the runs table |
| `reconcile()` | `queued`/`running` rows from a dead process → `interrupted`; called once at startup |
| `record_span(row)` | the handler's entry point; batched |
| `summary(window)` | the real-time tiles |
| `timeseries(bucket, window)` | the historical charts |
| `spans(run_id)` | the waterfall |
| `prune(before)` | retention, for when someone wants it |

`metrics/` imports `db`, `logger` and `compliance.schemas` and nothing else,
so nothing above it can be imported back into it. It sits beside `documents.py`
and `report.py` in the application layer.

## What the KPIs actually are

`00_overall_plan.md` lists them. One of them is already wrong, and that is
worth catching in a plan rather than in an interview:

> **cost/analysis (<$0.40, alert >$1)**

The measured cost of a five-criterion run on the sample contract is **$0.96**
(`.run/analyze_sample.jsonl`), and a single criterion live through the API was
**$0.134**. The target is unreachable at `analysis_effort=medium` on
`claude-opus-5` and the alert threshold fires on a normal run. Three honest
options, in the order I would take them:

1. **Re-target to `<$1.00`, alert `>$1.50`**, and say in the KPI doc that the
   number is what it is because the analysis runs at medium effort on the
   frontier model for a compliance judgement a lawyer would otherwise make.
2. **Measure a cheaper configuration** — `analysis_effort=low`, or the analysis
   finisher on a smaller model — and report both, which turns a missed target
   into a cost/quality curve, which is a better interview answer than either.
3. Leave the target and be seen to miss it. No.

The rest, with the thresholds from the overall plan and the source now that
the schema is known:

| KPI | Threshold | Source |
|---|---|---|
| e2e latency p50 / p95 | p50 < 90 s, alert p95 > 180 s | `runs.latency_s` |
| cost per analysis | see above | `runs.cost_usd` |
| **quote verification rate** | ≥ 95 %, alert < 90 % | `quotes_verified / quotes_total` — the direct hallucination signal |
| mean confidence | ≥ 0.75 | `runs.mean_confidence` |
| `needs_review` rate | < 20 % | `runs.needs_review / criteria_completed` |
| cap rate | low | `runs.capped` — how often a counter, not the model, ended a run |
| failure rate | < 2 % | `runs.status` |
| evaluator accept / revise / fallback | < 30 % revise, alert on any fallback | nullable until the evaluator lands |
| active / queued jobs | — | live from `JobRunner`, not from the table |
| retrieval health | empty retrievals = 0 | `spans` where `name='retrieve'` and `results=0` |
| spans dropped | 0 | the handler's counter |

Real-time tiles come from `summary(window)`; the historical charts from
`timeseries(bucket, window)`. Two of the tiles (active jobs, queued) are live
state and come from `JobRunner`, not the store — worth being explicit, because
they are the two a reader assumes are in the table.

## What this changes elsewhere

| File | Change |
|---|---|
| `logger.py` | `run_id` ContextVar, `run_context()`, stamped by `_ContextFilter`; `configure_logging(..., store=None)` installs the handler |
| `report.py` | `store=None` parameter; `start_run` / `end_run` / `fail_run`; sets `run_context(analysis_id)` |
| `api/main.py` | build the store in the lifespan, `reconcile()` at startup, close it at shutdown |
| `api/jobs.py` | pass the store to `analyze_document`; `JobState` becomes the live view over the row |
| `api/routes/analyses.py` | `GET /analyses/{id}` falls back to the row when the dict has no entry — a restart stops being a 404 |
| `api/routes/metrics.py` | four handlers stop raising and start returning |
| `api/errors.py` | the `analysis_not_found` hint loses its apology about restarts |
| `scripts/analyze.py` | pass a store, so the CLI populates the same tables |
| `docs/` | `metrics.md`; `architecture.md` rows; `api.md`'s "not here yet" section shrinks |

## Commit sequence

| # | Commit | What |
|---|---|---|
| 13a | `feat(logger): run_id in the trace context` | ContextVar, `run_context()`, filter, tests |
| 13b | `feat(metrics): the schema and the store` | `metrics.sql`, `MetricsStore` writes and reads, `reconcile` |
| 13c | `test: the store -- reconcile, totals, and what a deleted document leaves behind` | |
| 13d | `feat(metrics): spans from the logging handler` | queue, writer thread, drop counter, `configure_logging` wiring |
| 13e | `test: the handler -- every span lands, and a full queue drops instead of blocking` | |
| 13f | `feat(metrics): summary, timeseries and the span waterfall` | the query layer, percentiles in SQL |
| 13g | `feat(api): the metrics endpoints, and analyses that survive a restart` | `routes/metrics.py`, the durable read, lifespan |
| 13h | `test: api -- metrics over a real run, and a restart that keeps it` | |
| 13i | `docs(metrics): the KPI catalogue, thresholds, and the cost target we miss` | `docs/metrics.md`, `architecture.md` |

**Estimate: ~5 h.** I said "~1.5 h" when I proposed this — that was the store
alone, and it was wrong. The store is 1.5 h; the handler with its queue
discipline is another 0.75, the query layer with the percentile SQL and the
window parsing is 1, the API wiring 0.5, and the tests 1.25.

**Cut order if over budget:** `timeseries` (the tiles are the demo; the charts
are the nice-to-have) → `spans` table entirely, keeping `runs` and
`criterion_results` → the waterfall endpoint.

## Tests

Offline, no model, no network. The store is fed hand-built `AnalysisReport`s
and hand-built log records, which is enough for every claim here.

* `end_run` derives the totals from a report, and `report_json` round-trips
  back into an equal `AnalysisReport` — the no-second-schema claim.
* `quotes_verified` counts what `ResolvedQuote.verified` says, including the
  case where a quote was dropped for failing verification.
* `reconcile()` turns a `running` row from a dead process into `interrupted`,
  and leaves a `done` one alone.
* **Deleting a document leaves its runs.** The one that would be silently wrong
  with a foreign key, and the reason there isn't one.
* Two runs of the same document, one per criterion state → `summary` and
  `timeseries` bucket them correctly; `p50`/`p95` against a known list of
  latencies match hand-computed values, including the n=1 and n=2 edges where
  percentile arithmetic usually goes wrong.
* The handler writes one row per `span.end` and none per `span.start`; a
  malformed attribute does not raise; a full queue drops and increments the
  counter rather than blocking.
* `run_id` set by `analyze_document` appears on every span of that run and on
  none of an unrelated one sharing the trace.
* Through the API: run an analysis with the scripted model, then
  `/metrics/summary` reports one run with its cost, `/metrics/runs` lists it,
  `/metrics/runs/{id}/spans` returns a tree whose `parent_span_id`s resolve.
* A new `create_app` over the same database still serves `GET /analyses/{id}`
  from the row — the restart claim.

## Acceptance

- [ ] `make test` green, `make lint` clean; `metrics/` imports nothing above it.
- [ ] `make analyze F="data/samples/Sample Contract.pdf"` populates `runs`,
      `criterion_results` and `spans` — **from the CLI**, with no API involved.
- [ ] `GET /metrics/summary` after that run reports its latency, cost,
      quote-verification rate and mean confidence, and they match the report.
- [ ] `GET /metrics/runs/{id}/spans` returns a tree that reconstructs the run:
      `api.analysis` → `analysis.document` → five `analysis.criterion` →
      `agent.run` → `agent.call` / `agent.tool` → `retrieve`.
- [ ] Restart the API; `GET /analyses/{id}` for a finished run still returns
      its report, and a run that was `running` reads `interrupted`.
- [ ] `docs/openapi.json` regenerated; no operation answers `metrics_unavailable`.
- [ ] `spans_dropped` is 0 after a five-criterion run.

## Open questions

1. **The cost target.** Re-target to `<$1.00`, or measure a cheaper
   configuration and publish the curve? Recommendation: measure `low` effort
   once and publish both — it costs one run and turns a missed number into an
   argument.
2. **One file or two?** Recommendation: one. The whole storage story is "one
   file, no infrastructure", and a second file to explain is worse than a
   little WAL contention on writes that are batched and tiny. `METRICS_DB_PATH`
   as an escape hatch if the KPI page ever gets noisy.
3. **Should `chat` get a run row too?** Recommendation: no. A run is an
   analysis; chat's spans are queryable by `trace_id` and that is enough for
   "what does a question cost". Giving chat a `runs` row would make every
   analysis KPI need a `WHERE surface = 'analysis'`.
4. **Retention.** Recommendation: none for the demo, `prune(before)` present
   and untested-in-anger. Say so in the doc rather than pretending a demo has a
   retention policy.
5. **Does the handler belong in `logger.py` or `metrics/`?** Recommendation:
   the handler class in `metrics/`, installed by `configure_logging`. `logger.py`
   should not import `metrics`; `metrics` importing `logger` is the right
   direction.
