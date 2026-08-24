# Metrics

How a number on the KPI page becomes a query. The design that decided *which*
numbers is `plan_implement_docs/KPI_01/` — `00_README.md` for the dashboard
and the settled initial set, `01_findings.md` for grain, `02_costs.md` for the
cost family, and `Metric_Store.md` for the phased plan this module implements.

## The shape of it

```
metrics/
  windows.py   window/bucket arithmetic, and the empty buckets a chart needs
  queries.py   the SQL: summary, timeseries, runs
  store.py     MetricsStore -- what the surfaces hold
```

`metrics/` imports `db`, `logger` and nothing above them, and **nothing below
it imports `metrics`**. `analyses.py` records what happened without asking the
metrics module for permission; a store that could take storage down with it
would be a telemetry system with the priorities backwards.

## Phase 1: there is no new table

The dashboard's whole first cut is already in `analyses` (see
[storage.md](storage.md#analyses)). `analyses.py` fills the derived columns on
completion — `latency_s`, `cost_usd`, the token counts, `tool_calls`,
`needs_review`, `capped`, `mean_confidence`, `quotes_total`,
`quotes_verified`, `surface` — so the eight first-class KPIs are a `SELECT`
and a `COUNT(*)` on `documents`, with no schema change and no second source of
truth.

| KPI | Where it comes from |
|---|---|
| Failure rate | `status IN ('failed','interrupted')` over the settled runs |
| p50 / p95 latency | `latency_s`, percentiles in SQL |
| Cost per run, window spend | `cost_usd`: total, mean, p50, p95, and per bucket |
| Quote verification rate | `quotes_verified / quotes_total` |
| Needs-review rate | `needs_review / criteria_completed` |
| Mean confidence | `avg(mean_confidence)` |
| Cap rate | `capped / criteria_completed` |
| Runs count, `surface` split | `count(*)`, `GROUP BY surface` |
| Active now | `JobRunner`, **not** a table |

### Three decisions worth defending

**Three outcomes, not two.** `failed` (the job refused), `interrupted` (the
process died) and done-but-`needs_review` are reported separately. The first
two are reliability and the third is quality; a failure rate that absorbs the
third under-counts exactly what a compliance reviewer cares about.

**Percentiles in SQL, not in Python.** SQLite 3.51 has window functions, so
p95 is `row_number() OVER (ORDER BY latency_s)` against `count(*) OVER ()` —
nearest rank, `ceil(n·p/100)` written as `(n*p + 99) / 100` because `ceil()`
is a compile-time option in SQLite and the interpreter here is whichever one
Python was built against. Nothing is fetched into the API to be sorted. At
n=1 both percentiles are the single value; at n=2, p50 is the lower and p95
the upper.

**Rates return `null`, never `0.0`, for an empty set.** A quote-verification
rate of zero and no quotes at all mean opposite things, and every rate ships
with the counts it was computed from.

### The evaluator slot is honest

`analyses.evaluator_accepted / _revised / _fallback` are declared and `NULL`
until the evaluator lands. `summary` therefore reports **cap rate** in that
slot, inside an object that says so:

```json
"evaluator": {"available": false, "accept_rate": null,
              "showing": "cap_rate", "value": 0.1, "note": "..."}
```

A UI renders the note. When the evaluator lands, `available` flips and the
tile keeps its place.

### Windows and buckets move together

The window selector drives both: **24 h → `1h`, 7 d → `6h`, 30 d → `1d`**.
`timeseries` applies that pairing when the caller sends only a window.
Buckets are floored on the unix epoch, not on the request time, so the same
run lands in the same bar on every refresh — and **empty buckets are still
returned**, because a chart that closes its gaps draws a busy night out of a
quiet one.

## The endpoints

| Route | Answers |
|---|---|
| `GET /api/metrics/summary?window=` | The tiles and meters, plus `live` from the runner |
| `GET /api/metrics/timeseries?window=&bucket=` | One entry per bucket, oldest first |
| `GET /api/metrics/runs?limit=` | The global runs table, each row with its `trace_id` |
| `GET /api/metrics/runs/{id}/spans` | The waterfall. **`503`** until phase 2 |

A window is `\d+[hd]`; anything else is a `422` in the API's error envelope.
`503 metrics_unavailable` now means one thing only — the process could not
build a store. **An empty database is a `200` with zeroes and nulls on it**,
because "nothing has run yet" is a fact about the system and not a failure of
the endpoint.

The payloads are JSON objects rather than pydantic models on purpose: which
numbers the dashboard shows is the KPI plan's business, and pinning the shape
in `docs/openapi.json` would make every addition to the metric set a spec
change. The routes are declared, the failure envelope is declared, and the
body is documented here.

## Still to come

* **Phase 2, `spans`** — one row per `span.end`, written by a logging handler,
  which is what makes chat cost, cost per model and the per-run waterfall
  answerable. The telemetry already exists in `.run/app.jsonl`; nothing
  queries it.
* **Phase 3, `criterion_results`** — state mix per criterion over time, the
  drift signal, without mining `report_json`.
