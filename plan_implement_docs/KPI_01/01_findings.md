# KPI 01 — findings (granularity, inventory, storage)

Record of a design pass on metric grain, what the database already holds,
whether a new analytics table is needed, what the front end should compute,
and how `06_metrics_plan.md` relates. Complements `KPI_plan.md` (brainstorm),
`00_README.md` (dashboard design and the settled initial set), `02_costs.md`
(the cost family) and `Metric_Store.md` (the phased implementation plan this
pass concluded in). Does not replace any of them.

## 1. How granular should the metrics be?

**Display coarse. Store fine.** The interview grades every tile on the
dashboard, not every column in SQLite.

The assignment asks you to defend each KPI: why it exists, what threshold you
would set, and what you would *do* when it fires. That is a hard cap on
first-class numbers. **Six to eight headline metrics** is the right grain.
Twenty tiles looks thorough and then the hour is spent explaining why
empty-retrieval-rate on BM25 vs dense is “critical.”

### Two products, two granularities

Treat the **KPI dashboard** and **observability** as different layers that
share a store.

| Layer | Grain | Job | What the panel sees |
|---|---|---|---|
| Dashboard tiles | one number per **analysis** (or a window of analyses) | “Is the service healthy and is the compliance product trustworthy?” | 6–8 tiles + 3–4 trend charts |
| Historical / drill-down | **run → criterion → span** | “Why did this run look like that?” | runs table → span waterfall, not extra tiles |
| Logs | every LLM / tool / retry | reconstruct a request | `trace_id` in `.run/app.jsonl`, not the UI |

If a number cannot change an action (retry, page someone, stop shipping a
prompt, flag `needs_review`), it is not a KPI. It is a span attribute.

### The grain this system already has

Use this hierarchy; do not invent a fourth one.

1. **Analysis / job** — latency, cost, status, `needs_review`, capped, mean
   confidence, quote-verify rate. **Tile grain.** One contract, one row.
2. **Criterion** — state, calibrated confidence, coverage, attempts,
   evaluator verdict. Historical *state distribution per criterion* lives
   here. Tiles should **not** be “Password Management p95.”
3. **Span / agent step** — router vs extract vs evaluate, tokens, model,
   retries. Waterfall only. Per-agent latency *share* is a talking point, a
   bad default tile.
4. **Quote / evidence** — claimed vs verified. Aggregate to a **rate**, not a
   per-quote chart.

### What belongs on the dashboard

Four families the assignment named (cost, quality, latency, other). One or
two KPIs each. (The concrete first cut chosen from these families — eight
numbers, thresholds and actions — is `00_README.md` § the initial set.)

- **Latency:** p50 / p95 end-to-end of a finished analysis. Alert on p95, not
  on parse-ms. Sequential sample run ~187 s vs ~60 s parallel.
- **Cost:** USD per analysis (tokens as a secondary line). Alert on a budget,
  not on embedding-batch size. The overall-plan target of `<$0.40` is wrong
  against a measured ~$0.96 five-criterion run; re-target or publish a
  cost/quality curve (`06_metrics_plan.md`).
- **Quality (the compliance product):** quote-verification rate; evaluator
  accept / revise / fallback (`needs_review` until the evaluator lands);
  mean calibrated confidence and/or Low / `needs_review` share. These are
  the hallucination and calibration story. Front row.
- **Reliability:** failed / queued / interrupted jobs; LLM error / 429 /
  refusal rate; cap rate (`ended_by=cap`).

Historical: the same series over time, plus **compliance-state mix per
criterion** (same file hash flipping state is drift).

### What not to promote to a tile

- Per-sub-requirement coverage (keep on the result JSON / criterion
  drill-down).
- Per-retriever empty-hit rates as tiles (one “retrieval health” signal, or
  a log/alert, is enough).
- Parse / embed / hydrate_ms — milliseconds on this contract. Waterfall
  only, so you can say ingestion is not the bottleneck.
- Per-model token mix as four tiles. One stacked “cost share by agent”
  chart is plenty.
- Confidence histogram *and* mean *and* Low-share *and* a calibration
  scatter. Pick mean + Low/`needs_review`; histogram as the historical view
  of the same idea.

Do not mix **pipeline diagnostics** with **product SLOs**. Diagnostics
belong in spans for the live log walkthrough. SLOs belong on tiles with an
alert action in one sentence.

**Capture vs display:** store at the finest grain you will debug (span +
criterion). Aggregate up for tiles (`summary?window=24h`) and bucket for
history (`timeseries?bucket=1h`). Do not pre-aggregate only at tile grain
or you cannot answer “which criterion is dragging confidence.”

## 2. Against the `KPI_plan.md` brainstorm

The list is a good **ops backbone**: volume, cost, latency, success/error,
split by upload / analyze / chat. For this assignment it is **incomplete
where the panel will probe hardest**.

“No labeled accuracy” is not “no quality KPIs.” There are no gold labels.
There **are** process-quality signals that do not need a human:

| Signal already computed | Why it is a KPI | Not the same as |
|---|---|---|
| `quotes_verified / quotes_total` | Hallucination proxy: quotes must be verbatim in the evidence ledger | “The Fully Compliant label is legally correct” |
| `needs_review` rate | Structural validator gave up; human should look | Accuracy |
| `capped` / `ended_by=cap` | Loop hit tool/evidence limits; incomplete by design | A crash |
| Mean calibrated confidence + Low share | Confidence already cut by verify-ratio and missing sub-requirements | Calibration to ground truth |
| Compliance-state mix **per criterion** over time | Same file hash, different state = prompt/model drift | True prevalence of non-compliance |

When the evaluator lands, add accept / revise / fallback. Until then,
`needs_review` is that story.

### Too fine / duplicated in the brainstorm

- **API calls vs tokens vs cost:** keep all three in storage; put **cost per
  analysis** on the tile. Tokens explain the dollar; call count explains
  retries/caps.
- **Chat aggregate and average** for cost and tokens: pick **cost per
  turn** (and maybe p95 latency). Count × average is the same as aggregate
  if you also show count.
- **Document count and analysis count:** denominators and context, weak as
  headline KPIs.
- **Upload time:** useful in the waterfall. As a tile it is ~1 s next to a
  ~60 s analysis.

### Errors: three outcomes, not two

`failed` (the job refused) vs `interrupted` (process died) vs analysis
**succeeded but `needs_review`**. The last is a **quality** error, not an
HTTP error. Charting only upload/analysis success vs fail under-counts what
a reviewer should care about.

## 3. What the database already has vs does not

### Already in SQLite

**`documents`** — successful ingest only: count, `ingested_at`, pages,
chunks, hash, filename. `GET /health` already returns `documents`. Failed
uploads never become a row. **No** `elapsed` / cost column.

**`analyses`** — already a KPI table for analysis jobs:

| Brainstorm item | Column / fact |
|---|---|
| How many analyses | row count |
| Time per job | `job_duration_s` |
| Cost per job | `cost_usd` |
| Tokens per job | `input_tokens`, `output_tokens` |
| “API calls” (search/tools) | `tool_calls` — **tool executions**, not Anthropic HTTP round-trips |
| Success / error | `status`: `done` / `failed` / `interrupted` / `cancelled` / `queued` / `running` |
| When | `created_at`, `completed_at` |
| Quality (not on the brainstorm) | `needs_review`, `capped`, `mean_confidence`, `quotes_total`, `quotes_verified` |
| Who called | `surface` (`ui` / `cli` / `mcp` / `api`) |

`report_json` still has per-criterion state, confidence, quotes. Queryable
with `json_each` (the documents list already does this for
`last_analysis.states`), but it is a blob, not a first-class timeseries
table.

**Live, not history:** `GET /health` → `analyses_running`. In-flight jobs
also live in the process until they finish.

### In logs only (`.run/app.jsonl`)

Ingest timing (`ingest.file`, `IngestResult.elapsed`), parse/chunk/embed
breakdown, **every LLM call** (`agent.call`: tokens, cost, model,
`latency_ms`), **chat** (`chat` span), retrieval empty-hits. The panel can
grep these; the dashboard cannot until something copies `span.end` into a
table.

### Not queryable today

| Wanted | Status |
|---|---|
| Chat: counts, cost, tokens, latency, errors | **Not stored.** Chat is stateless: the API returns `AnswerResult` and logs a `chat` span. |
| Upload process time | Computed and logged; **not on `documents`**. |
| Upload success/error **rate over time** | Failures are HTTP errors, not rows. |
| LLM **request** count (vs `tool_calls`) | Only in `agent.call` log lines. |
| p50/p95, daily buckets | No query layer. `/api/metrics/*` exists in OpenAPI and returns **503**. |
| Evaluator accept/revise/fallback | Columns exist on `analyses`, always `NULL` until the evaluator lands. |
| Per-criterion history / span waterfall | Planned (`criterion_results`, `spans`); not in the DB yet. |

For the ops brainstorm, analysis is ~80% already in `analyses`. Chat and
ingest-error history are the holes. Quality KPIs are already on `analyses`
even though they were not on the brainstorm list.

## 4. New database? New table? Second schema file?

**Same SQLite file.** Do not add a generic `analytics` table. A second
schema *file* is optional and only worth it for telemetry DDL, not for
duplicating `analyses`.

### An `analytics` table

`analyses` is already the analysis fact table. A second table that copies
those columns is a second source of truth on every `finish_analysis` /
`fail_analysis`.

What *would* help is tables for things **`analyses` is not**:

| Table | Benefit | Needed for the ops brainstorm? |
|---|---|---|
| *(none extra)* | `SELECT` over `analyses` + `COUNT(documents)` | Analyze volume/cost/latency/errors, document count |
| `spans` (or `chat_turns`) | Chat and ingest duration/failures become queryable | Chat KPIs, upload time, upload error *history* |
| `criterion_results` | State mix per criterion without mining `report_json` | Not on the brainstorm; strong for the interview quality story |

A catch-all `analytics(event_type, json)` dump is worse than `spans`: you
lose typed columns, percentiles get painful, and you re-invent `analyses`
inside JSON.

### A separate `schema.sql`

**Do not split the database.** One file (`data/contracts.db`) is the demo
story.

A **second DDL file** (e.g. `metrics.sql`) is packaging, not architecture:

- `schema.sql` is run through `.format(dim=…)` for `chunks_vec`. Any `{` in
  SQL is a placeholder. Span DDL that uses `json_extract(attrs, '$.model')`
  does not belong in that file.
- `analyses` has no braces and is a domain object (like `documents`). It
  correctly lives in `schema.sql` so the API does not depend on a metrics
  module to persist reports.
- Intended line: **`schema.sql` = what happened** (`documents`, `chunks`,
  `analyses`). **`metrics.sql` = how it went** (`spans`, maybe
  `criterion_results`), applied by the metrics store, same connection.

If `/metrics/*` is implemented as queries over `analyses` only, **no second
schema file is required.** If `spans` is added (chat/ingest without a
dedicated `chat_turns` table), then `metrics.sql` on the same DB is
justified. `CREATE TABLE IF NOT EXISTS` so an old DB just grows tables.

## 5. Front end vs back end

**Front end (charts and chrome):**

- 7d / 30d (and 24h) **as query params** to `/metrics/summary?window=` and
  `/metrics/timeseries?window=&bucket=`
- Render tiles, line/bar charts, a runs table
- Format currency, seconds, percents; color vs thresholds from
  `settings.json` (or a thresholds map the API returns)
- Optional: pick a run and request `/metrics/runs/{id}/spans` for a
  waterfall

**Not the front end:**

- Aggregates (sum cost, p95, success rate). `GET /analyses` is **per
  `document_id` on purpose** — a global list was deferred to
  `/metrics/runs`. Pulling every report (~30 KB × N) into the browser to
  `reduce()` is the wrong grain, and React session state will under-count
  after a refresh.
- Chat metrics, unless they are persisted first. The UI transcript is
  session-only.
- Percentiles over all runs. SQLite 3.51 can do window-function p95; the
  API should return one number per bucket.

**Practical split:**

1. Tiles + 7d/30d trends from **SQL on `analyses` (+ document count)** —
   no new DB; metrics routes stop 503’ing.
2. If chat stays a KPI: persist turns **or** ingest `span.end` into
   `spans`; do not reconstruct chat from the React app.
3. Leave span waterfall as drill-down once `spans` exists; do not block
   the dashboard on it.

Historical **has** to be server-side. The window toggle is the only part
that is naturally a front-end concern.

## 6. Is `06_metrics_plan.md` this topic?

Yes — and this section's conclusions are now written down as
**`Metric_Store.md`**, the corrected successor that separates 06's still-live
half (phases 2–3 there) from what already shipped. Implement from that file,
not from 06. What follows is the autopsy that justified it.

That file **is** the implementation plan for the data layer: how KPI
numbers get into SQLite, how `/metrics/*` stops returning 503, and which
numbers the dashboard reads. It is **partly out of date**, and **broader
than the ops brainstorm**.

### Still live

- `spans` written from `span.end` log records (ingest, chat, retrieve, LLM
  calls) without touching eight modules
- `criterion_results` for per-criterion history
- Logging handler + queue (telemetry must not block an analysis)
- Percentile / window queries
- Making `/metrics/*` return real JSON
- KPI catalogue with honest cost thresholds
- One SQLite file (`METRICS_DB_PATH` only as an escape hatch)
- Chat does **not** get a run row — queryable via spans (`name='chat'`),
  so analysis KPIs do not need `WHERE surface = 'analysis'`

### Stale (do not follow)

The durable-analyses hotfix already shipped the table 06 called **`runs`**.
It is named **`analyses`**, it lives in **`schema.sql`**, and `reconcile` /
restart-safe `GET /analyses/{id}` are already done.

Dead on arrival: creating `runs` in `metrics.sql`; `start_run` / `end_run`
as the thing that *creates* the analysis record; “analyses die with the
process” as a reason to do the step; cut-order “keeping `runs` and
`criterion_results`.”

`metrics.sql` still makes sense **only for telemetry tables** (`spans`,
`criterion_results`), not for a second copy of `analyses`.

### Vs the brainstorm

| Brainstorm | 06 |
|---|---|
| Analyze cost / tokens / time / errors | Yes, from what is now `analyses` (the plan still says `runs.*`) |
| Document count | Weak; health already has a live count |
| Chat cost / tokens / latency | Yes, as **`spans` named `chat` / `agent.call`**, not a chat table |
| Upload time / upload errors | Yes, as **`ingest.*` spans** |
| Volume tiles + 7d/30d charts | `summary(window)` + `timeseries(bucket, window)` |
| Quality (quotes, confidence, review) | **First-class in 06**; absent from the brainstorm |
| Span waterfall | Extra; assignment-friendly for the live log walkthrough |

Implementing 06 *as written* would redo `analyses`. Implementing **the
remaining half** is how chat/ingest KPIs and a real `/metrics` API land
without an `analytics` table.
