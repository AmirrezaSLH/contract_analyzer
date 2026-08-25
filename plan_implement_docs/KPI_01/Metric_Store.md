# KPI 01 · the metric store

**Status: plan, 2026-08-24.** How every number on the dashboard becomes a
query: which ones already are, and what to build for the rest. This is the
corrected successor to the still-live half of `../06_metrics_plan.md` — that
file predates the durable-analyses hotfix and half of it describes work that
has since shipped under other names (`01_findings.md` §6 is the autopsy).
Where the two disagree, this file wins. The cost family it feeds is
`02_costs.md`; the tiles it feeds are `00_README.md`.

*Housekeeping: this file briefly held a stray copy of the Front End 02
build-and-ship doc while it was being parked; that content lives in git
history at `plan_implement_docs/Front_End_02/06_build_and_ship.md`.*

## 1. Status quo — what already exists

Four facts this plan builds on rather than re-creates:

1. **`analyses` is already the analysis fact table**, in `schema.sql`, with
   the derived KPI columns populated on completion: `latency_s`, `cost_usd`,
   `input_tokens`, `output_tokens`, `tool_calls`, `needs_review`, `capped`,
   `mean_confidence`, `quotes_total`, `quotes_verified`, `surface`, `status`,
   and the nullable `evaluator_*` trio. `reconcile()` and the restart-safe
   `GET /analyses/{id}` shipped with it (`05_02_durable_analyses_hotfix.md`).
2. **Every span already carries the numbers.** `agent.call` logs `surface,
   turn, model, effort, input_tokens, output_tokens, cost_usd, latency_ms,
   status`; `chat` logs `document_id, cost_usd, tool_calls, grounded`;
   `ingest.*` logs the stage timings; `retrieve` logs `mode, top_k, results`.
   All of it goes to `.run/app.jsonl` and nothing queries it.
3. **`/api/metrics/*` exists in the OpenAPI document and answers 503** —
   summary, timeseries, runs, and the per-run spans waterfall.
4. **`generation/pricing.py` is the single price table** every `cost_usd`
   flows through. Verified against published rates and corrected (Sonnet 5)
   on 2026-08-24.

## 2. The shape of the work: three phases

The one-sentence version is unchanged from 06: telemetry that already
exists gets a table. But half the table already arrived, so the work splits
cleanly:

| Phase | Unlocks | Needs |
|---|---|---|
| **1 — queries over `analyses`** | The entire initial KPI set (`00_README.md` § the initial set): failure rate, p50/p95 latency, cost totals and trend, quote verification, needs-review, mean confidence, cap rate, runs count, `surface` split | No schema change. A query layer and four route handlers |
| **2 — `spans`** | Chat cost/latency, cost per model, cost share by agent, retrieval health, ingest timing, the per-run waterfall, embedding cost once captured (`02_costs.md` tier 3) | `metrics.sql`, a logging handler, `run_id` in the trace context |
| **3 — `criterion_results`** | State mix per criterion over time — the drift signal (same file hash, different state) — and "which criterion drags confidence" without mining `report_json` | One table written by `finish_analysis`, backfillable from `report_json` |

Phase 1 is what stops the 503s and lights the dashboard. Phase 2 is what
makes the Tier-2 cost questions answerable. Phase 3 is separable and blocks
nothing.

## 3. Phase 1 — the query layer over `analyses`

No new storage. `summary(window)` and `timeseries(bucket, window)` are
`SELECT`s over `analyses` plus `COUNT(*)` on `documents`; `runs(limit)` is
the global list that `GET /analyses` deliberately does not serve.

* **Windows and buckets** move together, driven by the UI selector:
  24 h → `1h`, 7 d → `6h`, 30 d → `1d`.
* **Percentiles in SQL, not Python.** SQLite 3.51 ships with this project
  and has window functions: p95 is `row_number() over (order by latency_s)`
  against `count(*) over ()`. No numpy, no fetching rows to sort in the API.
* **Live tiles are not table reads.** Active/queued come from `JobRunner`
  and `/health`, worth stating because they are the two a reader assumes
  are stored.
* **Three outcomes, not two**: `failed`, `interrupted`, and
  done-but-`needs_review` stay distinct in `summary` — the last is a quality
  signal, not a reliability one (`01_findings.md` §2).
* The evaluator columns are `NULL` until the evaluator lands, so `summary`
  reports the **cap rate** in that slot and the API says so, rather than
  serving a fake accept rate (`00_README.md` § the initial set).

## 4. Phase 2 — `spans`

### The table (`metrics.sql`)

One row per `span.end`: `span_id` (PK), `parent_span_id`, `trace_id`,
`run_id`, `name`, `status`, `latency_ms`, `ts`, and promoted columns for
what every query touches — `surface`, `criterion`, `document_id`, `model`,
`input_tokens`, `output_tokens`, `cost_usd` — with `attrs` as JSON for the
rest. Indexes on `(run_id)`, `(trace_id)`, `(name, ts)`.
`parent_span_id` is what makes `/metrics/runs/{id}/spans` a waterfall.

**Why a second DDL file, same database.** `db.py` runs `schema.sql` through
`.format(dim=…)`, so every literal brace in that file is a template
placeholder — span DDL using `json_extract(attrs, '$.model')` cannot live
there. `metrics.sql` is applied by the store on the same connection,
`CREATE TABLE IF NOT EXISTS`, so an old database just grows tables. One
file on disk remains the whole storage story; `METRICS_DB_PATH` stays an
escape hatch only.

**No foreign keys.** `DELETE /documents/{id}` must not take KPI history
with it; history that vanishes when you tidy up is not history.

### The handler

A `logging.Handler` on the project root logger, installed by
`configure_logging`, turns `span.end` records into rows. This is the whole
trick, and its consequences are the argument:

* **The eight emitting modules change by zero lines** — `generation/`,
  `retrieval/`, `ingest/`, `compliance/` get no `record_span()` calls, and
  no future module has to remember one.
* **The CLI is instrumented for free**: `scripts/analyze.py` calls the same
  `configure_logging` as the API. A KPI page that only sees API traffic
  measures the surface, not the system.
* **The log file and the table cannot disagree** — they are the same
  records. The file stays the source of truth for a walkthrough; the table
  becomes the source of truth for an aggregate.

Telemetry must never hold up an analysis. Non-negotiable discipline:

* the handler pushes onto a **bounded** `queue.Queue` and **drops on
  overflow** — it never blocks a criterion thread to record that a
  criterion thread was busy;
* one **daemon writer thread** drains it and writes in batches;
* a drop increments a counter that `GET /metrics/summary` reports — a
  metrics system that silently loses data is worse than one that says so;
* **`emit()` never raises.** The queue put and the row build are wrapped
  explicitly; a malformed span attribute must not fail the run it
  describes.
* **Every span is stored, no sampling.** One analysis is ~70 rows; a
  thousand analyses are a few megabytes. A sampling knob nobody tunes is a
  knob set wrong during the demo.

The handler class lives in `metrics/`; `configure_logging` installs it.
`logger.py` must not import `metrics`; `metrics` importing `logger` is the
right direction.

### `run_id` in the trace context

A `ContextVar` beside `trace_id` in `logger.py`: set by `analyze_document`,
stamped by `_ContextFilter`, read by the handler. Without it, attributing a
span to a run means guessing from `trace_id`, and one trace legitimately
contains an upload *and* an analysis. This is what ties the waterfall
together; chat spans simply have no `run_id`, which is correct.

### The queries it unlocks

```sql
-- chat cost per turn (tile: aggregate; trend: bucket by ts)
SELECT cost_usd, latency_ms FROM spans WHERE name = 'chat';

-- cost per model, covering analysis AND chat in one pass
SELECT model, SUM(cost_usd) FROM spans
 WHERE name = 'agent.call' GROUP BY model;

-- retrieval health
SELECT COUNT(*) FROM spans
 WHERE name = 'retrieve' AND json_extract(attrs, '$.results') = 0;
```

This one-query-covers-both property is why cost-per-model waits for spans
instead of mining `report_json` (`02_costs.md` §2).

## 5. Phase 3 — `criterion_results`

One row per criterion per run, in `metrics.sql`: `run_id` + `criterion_id`
(PK together), `state`, `confidence`, `raw_confidence`, `needs_review`,
`ended_by`, `structure_rounds`, `tool_calls`, `cost_usd`, `quotes_total`,
`quotes_verified`, `latency_s`, `evaluator_verdict` (nullable). Written by
`finish_analysis` from the report it already holds; backfillable from
`report_json` with `json_each`, so it can land late without losing history.

This is the drift signal and the calibration story's raw material
(`raw_confidence` vs `confidence` per criterion over many runs). Not on the
dashboard's first cut; queryable is enough.

## 6. What not to build — the stale half of 06

Implementing `06_metrics_plan.md` as written would redo work. Dead on
arrival, with what replaced each:

| 06 says | Reality |
|---|---|
| Create `runs` in `metrics.sql` | Shipped as **`analyses` in `schema.sql`** |
| `start_run` / `end_run` create the run record | `create_analysis` / `finish_analysis` / `fail_analysis` already do, with the derived columns |
| `reconcile()` as new work | Shipped; runs at startup |
| "Analyses die with the process" as motivation | Fixed by the hotfix |
| Cut order "keep `runs` and `criterion_results`" | Inverted: `analyses` is a given, `spans` is the point, `criterion_results` is the optional one |
| Chat gets a run row (rejected there too) | Still no: chat is `spans WHERE name='chat'`, so analysis KPIs never need `WHERE surface != 'chat'` |

The `MetricsStore` surface shrinks accordingly: no run lifecycle methods.
What remains is `record_span(row)` (the handler's entry point, batched),
`summary(window)`, `timeseries(bucket, window)`, `spans(run_id)`, and
`prune(before)`. `metrics/` imports `db`, `logger` and nothing above them.

## 7. What changes where

| File | Change |
|---|---|
| `logger.py` | `run_id` ContextVar + `run_context()`, stamped by `_ContextFilter` |
| `metrics/` (new) | `metrics.sql`, the store, the handler |
| `report.py` | sets `run_context(analysis_id)` around a run |
| `api/main.py` | build the store in the lifespan, install the handler, close on shutdown |
| `api/routes/metrics.py` | four handlers stop raising and start returning |
| `scripts/analyze.py` | same store wiring, so the CLI populates the same tables |
| `docs/` | `metrics.md`; the "not here yet" section of `api.md` shrinks |

Ingestion's embed span gains `tokens` and `cost_usd` as the small separate
change described in `02_costs.md` §2 tier 3 — it rides the handler for
free once both exist.

## 8. Tests

Offline, no model, no network — hand-built reports and hand-built log
records prove every claim:

* The handler writes one row per `span.end` and none per `span.start`; a
  malformed attribute does not raise; a full queue drops and increments the
  counter rather than blocking.
* `run_id` set by `analyze_document` appears on every span of that run and
  on none of an unrelated one sharing the trace.
* `summary` / `timeseries` bucket two known runs correctly; `p50`/`p95`
  match hand-computed values including the n=1 and n=2 edges where
  percentile arithmetic usually goes wrong.
* Deleting a document leaves its runs and spans — the reason there is no
  foreign key.
* Through the API with the scripted model: `/metrics/summary` reports the
  run's cost and quote-verification rate matching its report;
  `/metrics/runs/{id}/spans` returns a tree whose `parent_span_id`s
  resolve: `api.analysis` → `analysis.document` → five
  `analysis.criterion` → `agent.run` → `agent.call` / `agent.tool` →
  `retrieve`.

## 9. Acceptance

- [ ] `make test` green; `metrics/` imports nothing above it.
- [ ] `make analyze F="data/samples/Sample Contract.pdf"` populates `spans`
      **from the CLI**, no API involved.
- [ ] `GET /metrics/summary` reports latency, cost, quote-verification and
      mean confidence for that run, matching the report; no `/api/metrics/*`
      operation answers `metrics_unavailable`.
- [ ] Chat one question; `spans` holds a `chat` row with its `cost_usd`, and
      per-model cost includes it.
- [ ] `spans_dropped` is 0 after a five-criterion run.
- [ ] Restart the API; nothing regresses (the durable-analyses guarantees
      predate this work and must survive it).

## 10. Open questions

1. **Evaluator.** Columns and the meter slot are reserved; accept/revise/
   fallback lights up when it lands. Until then cap rate stands in and the
   UI says so.
2. **Retention.** None for the demo; `prune(before)` present and honest
   about being untested in anger.
3. **The waterfall UI.** The endpoint is phase 2; the front-end view of it
   is unscheduled and is the best answer to "walk me through this run" in a
   live demo.
