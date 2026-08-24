# Step 12b — the durable analysis record (hotfix)

**Status: draft for review, 2026-08-24.** A fix to step 12, to land *before*
step 13 (`06_metrics_plan.md`). Small, and deliberately not a detour: the table
it creates is the one the metrics store was going to create anyway, in its
final shape.

## The bug

An analysis exists only in `dict[analysis_id, JobState]` on the running
process. Nothing about it reaches disk. Concretely, today:

| | |
|---|---|
| Restart the API | `GET /analyses/{id}` → `404`, with a hint that apologises for it |
| Restart the API | `GET /documents/{id}` → `analyses: []` for a contract analysed a minute ago |
| Either way | the report is **gone**. The API never writes one; only `scripts/analyze.py --out` does |
| `uvicorn --workers 2` | two dicts. Half the polls 404, `find_live` stops de-duplicating, and two identical submissions each cost a dollar |

The last one is latent rather than live — `docker/entrypoint.sh` runs a single
worker — but it is a trap laid for whoever first tries to scale the container,
and it fails silently rather than loudly.

The `documents` row is fine. `document_id`, `filename`, `content_hash`,
`page_count`, `spine_source` and `ingested_at` are all persisted. **The gap is
one-sided: the contract is durable, the work done on it is not.**

## The decision that makes this a hotfix and not a detour

`06_metrics_plan.md` already fixes this, as a side effect of building the
`runs` table — but that is a five-hour step with a logging handler, a queue, a
writer thread and a percentile query layer, and none of that is needed to stop
losing reports.

So: **create the table now, in its final shape, and populate only the columns
this fix needs.** Not a temporary table to be migrated later, and not a second
source of truth beside the metrics store. Step 13 then adds `spans` and
`criterion_results` beside it and fills in the columns it cares about.

Three consequences follow, and they are the parts to review.

### 1. It is called `analyses`, not `runs`

`06_metrics_plan.md` inherited the name `runs` from step 22 of the overall
plan. Now that a hotfix is going to create the table, the name has to be
settled, because renaming it after it exists is a migration.

The domain object is an **analysis**. The API mints `analysis_id`, the routes
are `/analyses`, the schema type is `Analysis`, the CLI writes
`analysis-<id>.json`. "Run" is metrics vocabulary for the same thing, and a
table called `runs` whose primary key is `analysis_id` charges a small
translation tax on every query anyone ever writes against it.

**Recommendation: `analyses`, with `analysis_id` as the primary key.**
`06_metrics_plan.md` gets edited to match. `spans.run_id` becomes
`spans.analysis_id`.

### 2. It lives in `schema.sql`, not `metrics.sql`

`06` proposed a separate `metrics.sql` because `db.py` runs
`SCHEMA_PATH.read_text().format(dim=dim)`, so every literal brace in
`schema.sql` is a format placeholder and a metrics DDL full of
`json_extract(attrs, '$.model')` would break on the first one. That reasoning
holds for `spans`.

It does not hold for this table. A plain `CREATE TABLE` has no braces, and more
importantly **`analyses` is not telemetry** — it is the durable record of a
domain object, exactly like `documents`. It belongs beside `documents` and
`chunks`, in the file that defines what this system stores.

The line to draw is: `schema.sql` holds *what happened*; `metrics.sql` holds
*how it went*. `analyses` is the first; `spans` and `criterion_results` are the
second. That is a cleaner split than "everything metrics-adjacent in one file",
and it means the API's storage never depends on the metrics module — metrics
stays genuinely optional.

`db.EXPECTED_TABLES` gains `"analyses"`.

### 3. `interrupted` becomes a status

`JobStatus` is `queued | running | done | failed | cancelled`. A row left
`running` by a killed process is none of those. Step 12's plan said to mark it
`failed: interrupted`, which loses the distinction between *the model refused*
and *the machine went away* — and those two want different KPI treatment and
different UI copy ("this analysis failed" vs "this analysis was interrupted;
run it again").

**Recommendation: add `interrupted`.** It is an additive enum change, the
OpenAPI document is regenerated anyway, and the alternative is a `failed` row
whose real meaning is hidden in a string.

## What gets written, and by whom

A new module, `src/contract_analyzer/analyses.py`, beside `documents.py` — the
same layer, the same shape: plain functions over a connection, no framework,
importable by the CLI.

| Function | |
|---|---|
| `queue_analysis(conn, analysis_id, document_id, filename, criteria, trace_id, surface)` | writes `queued` |
| `mark_running(conn, analysis_id)` | upsert, so a CLI run that never queued still gets a row |
| `finish_analysis(conn, analysis_id, report)` | status from the report, plus `report_json` |
| `fail_analysis(conn, analysis_id, error)` | |
| `get_analysis(conn, analysis_id)` | `None` for unknown, like `get_document` |
| `list_analyses(conn, document_id=None, limit=50)` | newest first |
| `reconcile(conn)` | `queued`/`running` → `interrupted`; returns how many |

**`report.py` writes the lifecycle, not `JobRunner`.** This is the step-12
invariant — the API contains no logic the CLI does not have — and it is the
whole reason `analyze_document` takes `analysis_id` already.
`analyze_document` calls `mark_running` on entry and `finish_analysis` /
`fail_analysis` on exit, on its own connection. `JobRunner.submit` additionally
calls `queue_analysis`, because the API has a state the CLI does not: accepted
but not yet started. `mark_running` is an upsert precisely so both paths work.

Result: **`make analyze` populates the same table the API does.** A report
produced from the command line is readable through `GET /analyses/{id}`, which
is a better demonstration of the invariant than any assertion about it.

### Columns

Identity and lifecycle, populated now:

`analysis_id` (PK), `trace_id`, `document_id`, `filename`, `surface`
(`api` | `cli`), `status`, `criteria_requested`, `criteria_completed`,
`criteria_skipped`, `error`, `report_json`, `created_at`, `started_at`,
`completed_at`.

Declared now, nullable, populated by step 13:

`latency_s`, `cost_usd`, `input_tokens`, `output_tokens`, `tool_calls`,
`needs_review`, `capped`, `mean_confidence`, `quotes_total`,
`quotes_verified`, `evaluator_accepted`, `evaluator_revised`,
`evaluator_fallback`.

Declaring them now costs nothing and removes an `ALTER TABLE` from the middle
of step 13. They are all derivable from `report_json`, so populating them later
is a backfill rather than a re-run. (Populating them *now* is about ten lines,
since `finish_analysis` is holding the report — see Open questions.)

**`document_id` carries no foreign key**, and `filename` is denormalised beside
it. `DELETE /documents/{id}` must not take the analyses with it: the report is
the deliverable, it is self-contained, and a report that vanishes because
someone tidied up the contract is the opposite of a record. Same reasoning as
`06_metrics_plan.md`, and there is a test for it.

`report_json` is the `AnalysisReport` verbatim — the same bytes
`scripts/analyze.py --out` writes, so there is still no second schema.
Measured: **29,809 bytes for a five-criterion report**. A thousand analyses is
~30 MB, which is a note in the docs and not a design problem.

## Reading it back

The dict stays. It holds what the row cannot: `stage`, live `progress`, the
cancel flag, the SSE subscribers. The row holds what the dict cannot: anything
at all after a restart.

**The dict wins where both have an answer**, because it is the live one:

| Endpoint | |
|---|---|
| `GET /analyses/{id}` | dict, else row. A finished analysis from a previous process returns its report. |
| `GET /analyses?document_id=` | union, dict entries preferred, newest first |
| `GET /documents/{id}` | same union for the `analyses` array |
| `DELETE /documents/{id}` | the `409` check becomes a query, so a running analysis in *another* worker also blocks it |
| `POST /analyses` | `find_live` stays in-memory. A duplicate can only be joined if this process owns its stream and its cancel flag; a row in another process is not something this one can hand back a live handle to. Documented, not pretended. |

`api/errors.py`'s `analysis_not_found` hint loses its apology about restarts.

## Honest limits after this fix

Durable is not distributed. With `--workers 2`, a second worker can **read** a
neighbour's analysis and its report, and it still cannot stream its events or
cancel it — `Broadcast` and the cancel `threading.Event` are per-process
objects. That is a real ceiling and the docs should say so rather than let
someone discover it by scaling the container. Making it distributed means a
broker, which is exactly what step 12 declined to do for a local demo.

## Commit sequence

| # | Commit | What |
|---|---|---|
| 12b-1 | `feat(db): the analyses table` | `schema.sql`, `EXPECTED_TABLES`, `analyses.py` |
| 12b-2 | `test: the analysis record -- reconcile, and a delete that keeps the report` | |
| 12b-3 | `feat(compliance): analyze_document records the analysis` | `report.py`; `scripts/analyze.py` gets a row too |
| 12b-4 | `feat(api): analyses survive a restart` | `JobRunner.submit`, the dict-then-row reads, `reconcile()` in the lifespan, `interrupted` in `JobStatus` |
| 12b-5 | `test: api -- a restart keeps the report, and an interrupted run says so` | |
| 12b-6 | `docs(api): what is durable, what is per-process` | `docs/api.md`, `docs/storage.md`, regenerate `openapi.json` |

**Estimate: ~2.5 h.** Schema and module 45 min, wiring 45 min, the read-back
merge 30 min, tests 1 h, docs 20 min. The tests are the largest part and the
part not to skip: the failure this fixes is invisible until a restart, which is
exactly the kind of bug that comes back.

**Cut order if it must be smaller:** the union in `GET /analyses` and
`GET /documents/{id}` (keep only `GET /analyses/{id}`, which is where the
report lives) → the `DELETE` conflict query, keeping the in-memory check.

## Tests

* A report written by `analyze_document` round-trips out of `report_json` as an
  equal `AnalysisReport` — the no-second-schema claim, again.
* `reconcile()` turns `queued` and `running` into `interrupted`, leaves `done`,
  `failed` and `cancelled` alone, and reports the count.
* **Deleting a document leaves its analyses and their reports.** The one that
  would be silently wrong with a foreign key.
* A cancelled run persists `status='cancelled'` with its partial report and its
  `skipped` ids.
* A failed run persists the error and no report.
* `make analyze` (no API) writes a row with `surface='cli'`, and that row is
  readable through `GET /analyses/{id}` in an app built over the same database
  — the invariant, demonstrated rather than asserted.
* Through the API: run an analysis, build a **second** `create_app` over the
  same database, and `GET /analyses/{id}` still returns the report while
  `GET /documents/{id}` still lists it.
* A row left `running` before the second app starts reads `interrupted`, not
  `failed`.
* The live dict wins: an analysis that is `running` in this process reports its
  `stage` and `progress`, which the row does not carry.

## Acceptance

- [ ] `make test` green, `make lint` clean.
- [ ] Start the API, run an analysis, `Ctrl-C`, start it again:
      `GET /analyses/{id}` returns the report and `GET /documents/{id}` lists
      it.
- [ ] Kill the API mid-analysis, restart: that analysis reads `interrupted`
      and no client is left polling a `running` job that will never finish.
- [ ] `make analyze F="data/samples/Sample Contract.pdf"` with the API stopped,
      then start the API: the run appears under its document.
- [ ] `DELETE /documents/{id}` after an analysis: the document is gone, the
      analysis and its report are not.
- [ ] `docs/openapi.json` regenerated with `interrupted` in the status enum.

## What this changes in `06_metrics_plan.md`

Edits to make when this lands, so the two plans do not disagree:

* `runs` → `analyses`; `spans.run_id` → `spans.analysis_id`; `run_id`
  ContextVar → `analysis_id` ContextVar.
* The table moves out of `metrics.sql`; that file holds `spans` and
  `criterion_results` only.
* Commit 13b shrinks to the derived columns and the query layer; `start_run` /
  `end_run` / `reconcile` are already built here.
* The acceptance item *"Restart the API; `GET /analyses/{id}` still returns its
  report"* moves to this plan.
* The estimate drops from ~5 h to ~4 h.

## Open questions

1. **Populate the derived columns now, or in step 13?** `finish_analysis` is
   holding the `AnalysisReport`, so `cost_usd`, `latency_s`,
   `mean_confidence`, `quotes_total` and `quotes_verified` are about ten lines
   away. Recommendation: **do it** — it is cheaper than a backfill, and it
   means the KPI page's headline tiles work off this table with no further
   write path. The columns that genuinely have to wait are the evaluator's.
2. **`ingested_at` is not ISO-8601.** `documents.ingested_at` is SQLite's
   `datetime('now')` — `2026-08-24 04:30:50` — while every timestamp the API
   mints is `2026-08-24T04:30:50+00:00`. The new table will use the API's
   format, so any query comparing the two is comparing formats. Recommendation:
   normalise on read in `documents._document()` rather than migrating the
   column, and note it in `docs/storage.md`.
3. **Should `POST /analyses` join a live analysis owned by another worker?**
   Recommendation: no, as above — it cannot hand back a stream or a cancel
   handle it does not own. Worth stating in `docs/api.md` beside the existing
   note about rate limiting.
4. **Retention.** ~30 KB per report. Recommendation: none for the demo; the
   number goes in the docs so nobody is surprised at ten thousand analyses.
