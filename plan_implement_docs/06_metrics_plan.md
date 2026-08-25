# Step 13 — the metrics store: what a run cost, and whether it was any good

**Status: as-built, 2026-08-24.** This is the design as shipped. It supersedes
the draft of the same name (which put a `runs` table in `metrics.sql` and
lifecycle methods on `MetricsStore`) and the still-wrong steps 22–23 and 26 of
`00_overall_plan.md`. The durable analysis record landed first, as
`05_02_durable_analyses_hotfix.md`; this step did not recreate it. Thresholds
and tiles are `KPI_01/00_README.md`; cost is `KPI_01/02_costs.md`;
`KPI_01/Metric_Store.md` is the phased write-up this file now matches. The
module doc is `docs/metrics.md`.

**The one-sentence version.** Every span this system emits already carries the
numbers a KPI needs, and `analyses` already holds the run. This step gives
spans a table, the run a query layer, and the four `/metrics/*` operations a
payload instead of a `503`.

## Why this shape

The original draft had three motivations. Two of them were already gone when
this shipped:

1. **`/metrics/*` answering `503`.** True then; false now. An empty database is
   a `200` with zeroes and nulls. `503 metrics_unavailable` means only that
   this process could not build a store.
2. **"Analyses die with the process."** Fixed by the hotfix. `JobState` is the
   live view; the row is the durable one. Restart `GET /analyses/{id}` is
   `analyses.get_analysis`, not a metrics-store fallback.
3. **The KPI dashboard is an assignment deliverable.** Still the reason this
   exists. Phase 1 lights the tiles off `analyses`. Phase 2 is what makes chat
   cost, cost per model, and the waterfall answerable. Phase 3 is the drift
   signal, queryable, not on the first cut.

## The central decision

**The store is a sink for telemetry that already exists, not a second
instrumentation pass, and it is not the analysis fact table.**

`schema.sql` holds *what happened* — `documents`, `chunks`, `analyses`.
`metrics.sql` holds *how it went* — `spans`, `criterion_results`. The API's
storage must not depend on the metrics module to persist a report, so
`analyses.py` never imports `metrics/`. A process that never built a store
still records the run.

Here is what a live run actually emits, read out of `.run/app.jsonl`:

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
| `ingest.{file,parse,chunk,embed,write}` | `path, chunks, pages, spine_source, model` — `ingest.embed` also carries `tokens` and `cost_usd` |

Every one of them carries `trace_id`, `span_id`, `parent_span_id`,
`latency_ms` and `status`, because `logger.span()` puts them there.

So `spans` is written by a **`logging.Handler` on the project's root logger**,
which turns `span.end` records into rows. The consequences are the argument:

* **Eight modules change by zero lines.** `generation/`, `retrieval/`,
  `ingest/`, `compliance/` get no `record_span()` calls, and no future module
  has to remember to add one.
* **The CLI is instrumented for free.** `scripts/analyze.py` installs the same
  store the API does. A KPI page that only sees HTTP traffic is measuring the
  surface, not the system.
* **Chat and ingest are captured too**, not just analyses — so "what does a
  question cost" is `spans WHERE name = 'chat'` without a second design.
* **The log file stays the source of truth for a walkthrough**, and the table
  becomes the source of truth for an aggregate. Neither replaces the other,
  and they cannot disagree, because they are the same records.

The rejected alternative is explicit `store.record_span()` calls at each site.
It is more direct to read at one call site and worse everywhere else.

Application code does not call `record_span` at all. The handler is the writer.

### What the handler must not do

Telemetry must never hold up an analysis:

* `emit()` pushes onto a **bounded** `queue.Queue` (`CAPACITY = 10_000`) and
  **drops on overflow** — it never blocks a criterion thread to record that a
  criterion thread was busy;
* one **daemon writer thread** drains it and writes in batches (`BATCH = 128`)
  on a connection of its own;
* a drop increments a counter that `GET /metrics/summary` reports as
  `spans.dropped`;
* **`emit()` never raises.** Building the row and putting it on the queue are
  each wrapped. A malformed span attribute must not fail the run it describes;
* **every span is stored, no sampling.** One analysis is ~70 rows.

The handler class lives in `metrics/handler.py`. `configure_logging` does
**not** take a `store=` argument — that would make the second call a no-op on
an already-configured logger. `logger.attach_handler` stamps `_ContextFilter`
onto the handler and adds it; `MetricsStore.install()` is what calls it.
`logger.py` does not import `metrics`; `metrics` importing `logger` is the
right direction.

## Design decisions

1. **`analyses` is written by `analyses.py`; `spans` is written by the
   handler.** A run is a business fact with a lifecycle — it exists as
   `queued` before anything is logged, and it has to be reconcilable after a
   crash. That shipped in the hotfix: `queue_analysis` / `mark_running` /
   `finish_analysis` / `fail_analysis` / `reconcile`. A span is a log line.
   Deriving runs from `analysis.document` spans was considered and rejected
   for the same reason as before; the table just is not called `runs` and does
   not live in `metrics.sql`.
2. **`analyze_document` opens and closes the analysis row.** Not `JobRunner`,
   and not `MetricsStore`. Both the CLI and the API call `analyze_document`;
   `surface` (`cli` | `api` | `ui` | `mcp`) is what differs. The store is
   installed around that call so spans land.
3. **The report JSON lives in the analysis row; the derived columns are an
   index over it.** `finish_analysis` already holds the report. The totals,
   quote counts, `needs_review`, `capped`, `mean_confidence` are filled there
   so the query layer has nothing to backfill for the first-cut KPIs.
4. **`metrics.sql` is a second DDL file on the same database.** `db.py` runs
   `schema.sql` through `str.format(dim=dim)`, so span DDL that mentions
   `json_extract(attrs, '$.model')` cannot live there. Applied by the store
   with `CREATE TABLE IF NOT EXISTS`. One file on disk remains the storage
   story. There is no `METRICS_DB_PATH`.
5. **No foreign key to `documents`**, on `analyses` or on `spans`.
   `DELETE /documents/{id}` must not take the KPI history with it.
   `document_id` and `filename` are denormalised onto the analysis row.
   `spans.run_id` is the analysis id, kept as `run_id` because that is the
   ContextVar name and the waterfall path (`/metrics/runs/{id}/spans`). It was
   not renamed to `analysis_id`.
6. **`run_id` is a `ContextVar`, beside `trace_id`.** Set by
   `analyze_document` (and by the job worker around the span that covers
   queueing); stamped by `_ContextFilter`; read by the handler. Chat spans
   have no `run_id`, which is correct: chat is not a run. One trace
   legitimately contains an upload *and* an analysis; without `run_id` the
   parse would sit inside the analysis waterfall.
7. **Percentiles in SQL, not in Python.** Nearest rank:
   `row_number() OVER (ORDER BY latency_s)` against `count(*) OVER ()`, with
   `ceil(n·p/100)` written as `(n * p + 99) / 100` because SQLite's `ceil()`
   is a compile-time option. At n=1 both percentiles are the one value; at
   n=2, p50 is the lower and p95 the upper. Reads run on the **caller's
   connection**; the writer thread owns exactly one of its own.
8. **Every span is stored. No sampling.**
9. **Evaluator columns exist and are nullable.** `summary` therefore reports
   **cap rate** in the evaluator slot and the payload says so
   (`evaluator.available = false`, `showing = "cap_rate"`). A tile that
   invents an accept rate is worse than an honest stand-in.
10. **Rates return `null`, never `0.0`, for an empty set.** A
    quote-verification rate of zero and no quotes at all mean opposite things.
    Every rate ships with the counts it was computed from.
11. **Three outcomes, not two.** `failed`, `interrupted`, and
    done-but-`needs_review` stay distinct. The first two are reliability; the
    third is quality.
12. **Chat does not get an `analyses` row.** A run is an analysis. Chat is
    queried as `spans WHERE name = 'chat'`, so every analysis KPI never needs
    `WHERE surface != 'chat'`.
13. **`criterion_results` is written by `finish_analysis`, guarded.** The
    table is created by the metrics store. The INSERT lives in `analyses.py`
    and treats a missing table as not an error, so an analysis never fails to
    record itself because nobody asked for a dashboard. Backfill is
    `MetricsStore.backfill_criteria` (`json_each` over `report_json`,
    `INSERT OR IGNORE`).

## Schema

### `analyses` — one per `analyze_document` call (`schema.sql`)

Not in this step's DDL. Primary key `analysis_id`. Columns the query layer
reads: `trace_id`, `document_id`, `filename`, `surface`, `status` (`queued` →
`running` → `done` | `failed` | `cancelled` | `interrupted`),
`criteria_requested/completed/skipped`, `latency_s`, `cost_usd`,
`input_tokens`, `output_tokens`, `tool_calls`, `needs_review`, `capped`,
`mean_confidence`, `quotes_total`, `quotes_verified`,
`evaluator_accepted/revised/fallback` (nullable), `error`, `report_json`,
`created_at`, `started_at`, `completed_at`.

`quotes_total` / `quotes_verified` are counted from `ResolvedQuote.verified`
in `finish_analysis`, not recomputed by the dashboard from the blob.

### `spans` — one per `span.end` (`metrics.sql`)

`span_id` (PK), `parent_span_id`, `trace_id`, `run_id`, `name`, `status`,
`latency_ms`, `ts`, plus the attributes worth having as columns because
everything queries them — `surface`, `criterion`, `document_id`, `model`,
`input_tokens`, `output_tokens`, `cost_usd` — and `attrs` as JSON for the
rest. Indexes on `(run_id)`, `(trace_id)`, `(name, ts)`.

`parent_span_id` is what makes `/metrics/runs/{id}/spans` a tree rather than
a list. The route returns the tree already resolved; a run with no spans is
an empty list, not a 404.

### `criterion_results` — one per criterion per run (`metrics.sql`)

`(run_id, criterion_id)` primary key. `state`, `confidence`,
`raw_confidence`, `needs_review`, `ended_by`, `structure_rounds`,
`tool_calls`, `cost_usd`, `quotes_total`, `quotes_verified`, `latency_s`,
`evaluator_verdict` (nullable). No `input_tokens` / `output_tokens` /
`created_at` — those live on the analysis row and on `agent.call` spans.

This is what makes "state distribution per criterion" and "which criterion
is expensive" one query instead of mining `report_json`. It is also the
drift signal: the same document hash producing a different state for one
criterion across runs. Not on the dashboard's first cut.
`MetricsStore.criterion_mix` is the query; there is no endpoint.

## `MetricsStore` (`metrics/store.py`)

One per process, built in the API lifespan and by `scripts/analyze.py`.
Constructor applies `metrics.sql`. `install()` attaches the handler and
starts the writer; building a store to *read* does not start a thread.

| Method | |
|---|---|
| `install()` / `close()` | handler on, handler off, flush |
| `dropped` / `flush()` | the drop counter, and a wait for tests and the CLI |
| `summary(conn, window)` | the tiles; caller merges `live` from `JobRunner` |
| `timeseries(conn, window, bucket)` | historical charts; empty buckets included |
| `runs(conn, limit)` | global list, newest first, each row with its `trace_id`. No `document_id` filter — that list is `GET /analyses?document_id=` |
| `spans(conn, run_id)` | the waterfall tree |
| `criterion_mix(conn, window)` | drift / calibration; not an HTTP route |
| `backfill_criteria(conn)` | recover `criterion_results` from stored reports |
| `prune(conn, before)` | delete old spans; leaves `analyses` alone. No retention policy for the demo; never run in anger |

There is no `start_run` / `end_run` / `fail_run` / `reconcile` / `get_run` /
`record_span` on this object. Those either shipped in `analyses.py` or are
the handler.

`metrics/` imports `db`, `logger`, `config` and nothing above them.

## What the KPIs actually are

The overall-plan line `cost/analysis (<$0.40, alert >$1)` is wrong against a
measured **~$0.96** five-criterion run on the sample contract at medium
effort on the frontier model. That target was not kept, not quietly missed,
and not replaced with a `$1.00` / `$1.50` pair. The settled set is
`KPI_01/00_README.md`, and `ui/src/views/Metrics/thresholds.ts` copies it:

| KPI | Threshold | Source |
|---|---|---|
| quote verification | ≥ 99% | `quotes_verified / quotes_total` |
| needs-review rate | ≤ 10% | `needs_review / criteria_completed` |
| mean confidence | trend only | `avg(mean_confidence)` |
| failure rate | ≤ 2% | `failed + interrupted` over settled runs — never absorbs needs-review |
| latency p95 | ≤ 120 s | `runs.latency_s`; ~60 s parallel measured, headroom on top |
| cost per run | ~$0.96 measured | `cost_usd` mean / p50 / p95; no `$0.40` target |
| window spend | $50/day budget | sum of `cost_usd`; a breach pauses new runs (`02_costs.md`) |
| cost tail | p95 ≤ 2× p50 | a ratio, not a dollar cap |
| cap rate | stand-in for evaluator accept | `capped / criteria_completed` until evaluator columns fill |
| evaluator accept | ≥ 85% when it lands | nullable columns; payload says which number the slot is showing |
| active / queued | — | `JobRunner.live`, merged in the route |
| chat cost / latency | — | `spans WHERE name = 'chat'` |
| cost by model | — | `spans WHERE name = 'agent.call'` |
| spans dropped | 0 | the handler's counter |
| retrieval health | empty retrievals = 0 | queryable on `spans`; not a first-cut tile |

Real-time tiles come from `summary(window)`; the historical charts from
`timeseries(bucket, window)`. Windows and buckets move together: **24 h →
`1h`, 7 d → `6h`, 30 d → `1d`**. Buckets are floored on the unix epoch, not
the request. Embedding cost is captured on `ingest.embed` (~$0.0002 on the
sample) and **never tiled**.

The waterfall endpoint is shipped. The front-end view of the tree is not a
first-cut band; cost-by-model and chat aggregates on the KPI page are.

## What this changed

| File | What it does |
|---|---|
| `logger.py` | `run_id` ContextVar, `run_context()`, `_ContextFilter`, `attach_handler` / `detach_handler` |
| `metrics/` | `windows.py`, `queries.py`, `handler.py`, `metrics.sql`, `store.py` |
| `analyses.py` | `finish_analysis` fills derived columns and guarded `criterion_results` |
| `report.py` | `run_context(analysis_id)` around the run; `mark_running` / `finish_analysis` / `fail_analysis` on `conn` |
| `api/main.py` | build and install the store in the lifespan; `reconcile()` still on `analyses`; close the store at shutdown |
| `api/routes/metrics.py` | four handlers return pydantic payloads (`schemas.py`) |
| `api/jobs.py` | pass-through: the worker already called `analyze_document`; `run_context` also around `api.analysis` |
| `scripts/analyze.py` | `MetricsStore(...).install()`, flush before exit |
| `docs/metrics.md` | the module doc |

## Tests

Offline, no model, no network, in `tests/test_metrics.py`. Hand-built
`analyses` rows, hand-built log records, and a scripted-model API client.

* `summary` / `timeseries` bucket known runs; `p50`/`p95` match hand-computed
  values at n=1, n=2, and n=10. A run with no latency is not a zero in the
  percentile. Empty windows are zeroes and nulls, not a failure. Empty
  buckets are returned rather than closed up.
* Failure rate is `failed + interrupted` and excludes needs-review. A queued
  run is not in the denominator. Rates carry their denominators. The
  evaluator slot says it is showing cap rate.
* The handler writes one row per `span.end` and none per `span.start`; a
  malformed attribute does not raise and does not lose the span; a full queue
  drops and increments the counter rather than blocking. The counter is on
  `summary`.
* `run_id` set around a block appears on that run's spans and on none of an
  unrelated one sharing the trace. Chat is a span query and never a run row.
  Cost per model covers analysis and chat in one pass.
* Deleting a document leaves its analyses and its spans.
* The waterfall is a tree whose `parent_span_id`s resolve. A run with no
  spans is an empty tree, not an error. `prune` drops old spans and leaves
  `analyses` alone.
* Through the API with the scripted model: `/metrics/summary` matches the
  report; `/metrics/runs` lists it with its trace; `/metrics/runs/{id}/spans`
  reconstructs `api.analysis` → `analysis.document` → criteria → `agent.*` →
  `retrieve`. The CLI populates `spans` with no API involved. A finished run
  writes one `criterion_results` row per criterion. An analysis still records
  itself when the metrics tables are absent. `backfill_criteria` recovers
  history that predates the table.

## Acceptance (shipped)

- [x] `metrics/` imports nothing above `db` / `logger` / `config`.
- [x] `scripts/analyze.py` populates `analyses`, `criterion_results` and
      `spans` — from the CLI, with no API involved.
- [x] `GET /api/metrics/summary` reports latency, cost, quote-verification
      rate and mean confidence matching the report. An empty database is 200,
      not `metrics_unavailable`.
- [x] `GET /api/metrics/runs/{id}/spans` returns a tree that reconstructs the
      run.
- [x] Restart the API: finished `GET /analyses/{id}` still returns the
      report (hotfix); a row left `running` reads `interrupted`. Metrics
      queries survive it.
- [x] Chat is in `spans` with `cost_usd`; per-model cost includes it.
- [x] `criterion_mix` is queryable; not an endpoint.
- [x] `spans.dropped` is reported; a five-criterion run is expected to leave
      it at 0.

## Open questions (still open)

1. **Evaluator.** Columns and the meter slot are reserved. Until it lands,
   cap rate stands in and the UI says so.
2. **Retention.** None for the demo. `prune(before)` exists and is tested
   against fixtures, not production volume.
3. **The waterfall UI.** The endpoint is shipped; a dedicated per-run view of
   the tree is still the best unanswered "walk me through this run" in a live
   demo. Cost-by-model on the KPI page is the part that did ship.
4. **Retrieval health as a tile.** Empty `retrieve` spans are queryable.
   They are not on the first cut.
5. **`METRICS_DB_PATH`.** Rejected. One file is the storage story; a second
   file to explain is worse than WAL contention on writes that are batched
   and tiny.
