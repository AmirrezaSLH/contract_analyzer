# Metrics

How a number on the KPI page becomes a query. The design that decided *which*
numbers is `plan_implement_docs/KPI_01/` — `00_README.md` for the dashboard
and the settled initial set, `01_findings.md` for grain, `02_costs.md` for the
cost family, and `Metric_Store.md` for the phased plan this module implements.

## The shape of it

```
metrics/
  windows.py   window/bucket arithmetic, and the empty buckets a chart needs
  queries.py   the SQL: summary, timeseries, runs, spans
  stages.py    Monitor: worst pipeline stage, GROUP BY name over spans
  host.py      Monitor: latest RAM/disk sample, last-in-bucket series
  sampler.py   30s thread writing system_samples (API process only)
  handler.py   span.end log records -> rows, on a queue and a writer thread
  metrics.sql  the spans DDL, applied by the store on the same database
  store.py     MetricsStore -- what the surfaces hold
```

`metrics/` imports `db`, `logger` and nothing above them, and **nothing below
it imports `metrics`**. `analyses.py` records what happened without asking the
metrics module for permission; a store that could take storage down with it
would be a telemetry system with the priorities backwards.

## Phase 1: the tiles need no new table

The dashboard's whole first cut is already in `analyses` (see
[storage.md](storage.md#analyses)). `analyses.py` fills the derived columns on
completion — `job_duration_s`, `cost_usd`, the token counts, `tool_calls`,
`needs_review`, `capped`, `mean_confidence`, `quotes_total`,
`quotes_verified`, `surface` — so the first-class KPIs are a `SELECT` and a
`COUNT(*)` on `documents`, with no schema change and no second source of
truth.

| KPI | Where it comes from |
|---|---|
| Runs | `count(*)` on `analyses` in the window |
| Failure rate | `status IN ('failed','interrupted')` over the settled runs |
| p50 / p95 job duration | `job_duration_s`, percentiles in SQL |
| Total spend | `SUM(cost_usd)` on analysis rows in the window |
| p50 / p95 job cost | `cost_usd` percentiles in SQL |
| Quote verification rate | `quotes_verified / quotes_total` |
| Needs-review rate | `needs_review / criteria_completed` |

`GET /metrics/summary` still carries `live` from `JobRunner` (workers busy and
queued) and a `GROUP BY surface` split. The KPI page does not tile either:
busy/queued is a fact about this process, not the window, and surface (`api` /
`cli`) is who submitted an analysis, not analysis vs chat. Chat cost is
`spans WHERE name = 'chat'` and is drawn as its own share of billed spend.

### Three decisions worth defending

**Three outcomes, not two.** `failed` (the job refused), `interrupted` (the
process died) and done-but-`needs_review` are reported separately. The first
two are reliability and the third is quality; a failure rate that absorbs the
third under-counts exactly what a compliance reviewer cares about.

**Percentiles in SQL, not in Python.** SQLite 3.51 has window functions, so
p95 is `row_number() OVER (ORDER BY job_duration_s)` against `count(*) OVER ()` —
nearest rank, `ceil(n·p/100)` written as `(n*p + 99) / 100` because `ceil()`
is a compile-time option in SQLite and the interpreter here is whichever one
Python was built against. Nothing is fetched into the API to be sorted. At
n=1 both percentiles are the single value; at n=2, p50 is the lower and p95
the upper.

**Rates return `null`, never `0.0`, for an empty set.** A quote-verification
rate of zero and no quotes at all mean opposite things, and every rate ships
with the counts it was computed from.

### Windows and buckets move together

The window selector drives both. KPI and Monitor share the same pairing:

| Window | Bucket | About how many points |
|---|---|---|
| 30 m | `1m` | 30 |
| 1 h | `1m` | 60 |
| 24 h | `1h` | 24 |
| 7 d | `6h` | 28 |
| 30 d | `1d` | 30 |

`timeseries` applies that pairing when the caller sends only a window. Host
charts are the exception: 30 m / 1 h stay one point per `monitor_sample_seconds`
tick so the sampler's grain is visible; 24 h / 7 d / 30 d follow the table
above so a week is not tens of thousands of 30-second points.

Buckets are floored on the unix epoch, not on the request time, so the same
run lands in the same mark on every refresh — and **empty buckets are still
returned**, because a chart that closes its gaps draws a busy night out of a
quiet one. **The last bucket is the current one and is partial.**

`/metrics/*` accepts any `window` matching `\d+[mhd]` (`30m`, `1h`, `24h`,
`7d`, `30d`). `/monitor/*` accepts exactly those five. Anything else is a
`422` in the API's error envelope.

## The endpoints

| Route | Answers |
|---|---|
| `GET /api/metrics/summary?window=` | The tiles and meters. `live` is merged from `JobRunner`; it is not a table read. |
| `GET /api/metrics/timeseries?window=&bucket=` | One entry per bucket, oldest first. Each carries `runs`, `job_duration_s` p50/p95, `cost_usd` (sum) and `cost_percentiles`. |
| `GET /api/metrics/runs?limit=` | The global runs table, each row with its `trace_id` |
| `GET /api/metrics/runs/{id}/spans` | One run's span tree, for the waterfall |
| `GET /api/monitor/stages?window=` | Worst `span` name over five minutes, its error rate, and the error-count trend across named stages |
| `GET /api/monitor/host?window=` | Latest process memory and disk used %, one point per sampler tick (short windows) or the paired bucket (long windows) |
| `GET /api/monitor/upstream?window=` | Retries per 100 outbound calls, exhausted rate, and the top retry reason |

Monitor upstream is a query over `spans` named `upstream.call`,
`upstream.retry`, and `upstream.failed`. `RetryingTransport` emits those at
the same site as `http.retry` / `http.failed`. Tiles use the last five minutes
when anything was called; otherwise the chart window. `top_reason_share` is
that reason's share of retry + exhausted events, not of all calls.

`503 metrics_unavailable` now means one thing only — the process could not
build a store. **An empty database is a `200` with zeroes and nulls on it**,
because "nothing has run yet" is a fact about the system and not a failure of
the endpoint.

Monitor host is not a span query. A daemon sampler in the API process writes
`system_samples` every `monitor_sample_seconds` (30 by default): `VmRSS` as a
share of `MemTotal`, and `shutil.disk_usage` of the database directory. HTTP
columns on that table stay NULL until the request ring lands. Tiles are the
latest row; charts take the last sample in each `monitor_sample_seconds`
bucket on 30m/1h, and in each paired bucket on 24h / 7d / 30d. `make analyze`
does not start the sampler — "is the box healthy" is not a laptop question.

The payloads are pydantic models, so they appear in `docs/openapi.json` and
the front end's types are generated from that. The KPI page draws three line
charts from `timeseries` — runs initiated (a quiet hour is zero, not a gap),
job-duration p50 and p95, job-cost p50 and p95 — each on its own axis. A
null percentile **breaks the line**; coercing it to zero would draw a cliff
on every empty bucket.

## Phase 2: `spans`

Three questions `analyses` structurally cannot answer — **chat cost**, **cost
per model**, and **"walk me through this run"** — and all three are already in
`.run/app.jsonl`. Nothing queried it. Now a handler copies every `span.end`
record into a table.

### The handler is the whole trick

`SpanHandler` is a `logging.Handler` on the project's root logger, installed by
`MetricsStore.install()`. What that buys:

* **The eight emitting modules change by zero lines.** `generation/`,
  `retrieval/`, `ingest/` and `compliance/` already wrap their work in
  `span()`; none of them gained a `record_span()` call, and no module written
  after this one has to remember one.
* **The CLI is instrumented for free.** `scripts/analyze.py` builds the same
  store, so `make analyze` populates `spans` with no API involved. A KPI page
  that only saw HTTP traffic would be measuring the surface, not the system.
* **The log file and the table cannot disagree**, because they are the same
  records.

### Telemetry never holds up an analysis

Not a preference — the constraint the design is shaped by:

| Rule | Why |
|---|---|
| `emit()` pushes onto a **bounded** queue and **drops on overflow** | It must never block a criterion thread in order to record that a criterion thread was busy |
| A drop **increments a counter** `/metrics/summary` reports as `spans.dropped` | A metrics system that silently loses data is worse than one that says it lost some |
| One **daemon writer thread**, batched, on its own connection | A writer cannot borrow a request's connection, and a request must not wait for a writer |
| **`emit()` never raises** | A malformed span attribute must not fail the run it describes |
| **No sampling** | One analysis is ~70 rows; a sampling knob nobody tunes is a knob set wrong during the demo |

### `run_id`, beside `trace_id`

A `ContextVar` in `logger.py`, set by `analyze_document` (and by the API's job
worker around the span that covers queueing). Without it, attributing a span to
a run means guessing from the trace — and one trace legitimately contains an
upload *and* an analysis, so `make analyze path.pdf` would file its parse
inside the analysis's waterfall. **Chat spans simply have no `run_id`**, which
is correct: chat is not a run.

### The table

`metrics.sql` is a second DDL file on the **same database**. It is a separate
file for a mechanical reason as well as a conceptual one: `db.py` runs
`schema.sql` through `str.format(dim=…)`, so every literal brace there is a
format placeholder, and span DDL reading `json_extract(attrs, '$.model')`
cannot live in it. `CREATE TABLE IF NOT EXISTS`, so an old database just grows
the tables.

`span_id` (PK), `parent_span_id`, `trace_id`, `run_id`, `name`, `status`,
`latency_ms`, `ts`; `surface`, `criterion`, `document_id`, `model`,
`input_tokens`, `output_tokens`, `cost_usd` promoted into columns because
every query touches them; everything else as `attrs` JSON. Indexes on
`(run_id)`, `(trace_id)`, `(name, ts)`.

**No foreign keys.** `DELETE /documents/{id}` must not take the KPI history
with it — history that vanishes when somebody tidies up the corpus is not
history. The same argument as `analyses`, and the suite asserts the outcome.

### What it unlocks

```sql
-- chat cost per turn: chat writes no row anywhere, by design
SELECT cost_usd, latency_ms FROM spans WHERE name = 'chat';

-- cost per model, covering analysis AND chat in one pass
SELECT model, SUM(cost_usd) FROM spans WHERE name = 'agent.call' GROUP BY model;
```

Cost per model waited for this instead of mining `report_json` precisely
because one query covers both analysis and chat. `summary` gains `chat` and
`cost_by_model`; `timeseries` gains chat turns and cost per bucket, plus
`cost_percentiles` (p50/p95 of analysis `cost_usd` in that bucket). The KPI
cost band splits billed spend into **Chat** (`spans` named `chat`) and
**Analysis** (`SUM(analyses.cost_usd)`), not into `api`/`cli` surfaces.
`GET /metrics/runs/{id}/spans` returns the tree:

```
api.analysis -> analysis.document -> analysis.criterion (x5)
             -> agent.run -> agent.call / agent.tool -> retrieve
```

A tree rather than a flat list, because resolving `parent_span_id` in the
browser would mean writing the same algorithm again in TypeScript. A run with
no spans is an empty list, not a 404 — it may be a run from before this table
existed, and it is still in `/metrics/runs` beside it.

### Embedding cost: captured, never tiled

`ingest.embed` now carries `tokens` and `cost_usd`, read from the embeddings
response's `usage.total_tokens` and priced by `EMBEDDING_PRICES` in
`generation/pricing.py`. It rides the handler into `spans` like everything
else.

There is deliberately **no tile for it**. At $0.02/1M, embedding the 21-page
sample costs about **$0.0002** against the **~$0.96** analysis it enables —
four orders of magnitude, so a dashboard tile would be four leading zeros.
What the capture buys is the sentence the waterfall can then make: *ingestion
costs a fiftieth of a cent; the dollar is all reasoning.* The local and fake
embedders report no tokens and price at $0.00, which is the truth and not a
missing number.

A related footnote worth keeping on the page: `pricing.py` prices an **unknown
model at $0.00** and logs `pricing.unknown_model` once. So a `$0.00` average
cost has a known failure meaning — a model id the price table has not learned
— rather than a free run.

### Retention

`prune(before)` deletes spans older than a timestamp and leaves `analyses`
alone. There is no retention policy for the demo, and this has never been run
in anger; it exists so that "what happens when the table grows" has an answer
that is not "nobody thought about it".

## Phase 3: `criterion_results`

One row per criterion per run — the grain between `analyses` (one row per run)
and `spans` (one row per step). `finish_analysis` writes it from the report it
is already holding, so there is no second pass and no second source of truth;
the columns are `state`, `confidence`, `raw_confidence`, `needs_review`,
`ended_by`, `structure_rounds`, `tool_calls`, `cost_usd`, `quotes_total`,
`quotes_verified`, `duration_s`, and an `evaluator_verdict` that is `NULL` until
the evaluator lands.

It answers the two questions `report_json` answers badly:

* **Drift** — the same file hash coming back with a different compliance
  state. Finding that in a blob means mining every report on every query.
* **Calibration** — `raw_confidence` (the model's own estimate) against the
  derived `confidence`, per criterion, over many runs.

**The write is guarded.** `criterion_results` is created by the metrics store,
and `analyses.py` must not import `metrics` — storage does not depend on
telemetry. A process that never built a store records no per-criterion history
and logs that at debug level; an analysis never fails to record itself because
nobody asked for a dashboard.

**Backfillable.** `MetricsStore.backfill_criteria(conn)` fills the table from
reports already on disk with `json_each`, `INSERT OR IGNORE`, which is why this
table could land last without losing history.

**Not on the dashboard.** Five criteria times three states is fifteen numbers —
a drill-down, not a tile. `MetricsStore.criterion_mix(conn, window=…)` is the
query; there is no endpoint, because `00_README.md` defers it off the first
cut. Queryable is what this phase promised.
