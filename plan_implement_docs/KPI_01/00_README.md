# KPI 01 — the dashboard, designed

**Status: design done, initial metric set settled, 2026-08-24.** The KPI
dashboard exists as a working design and the first-cut metrics are chosen
(§ the initial set). This document is the pointer to the design and the
record of what it shows and why. `KPI_plan.md` is the raw brainstorm it grew
from. Grain and what SQLite already holds: `01_findings.md`. The cost family
in detail: `02_costs.md`. How every number becomes queryable — the phased
implementation plan that supersedes the stale half of `06_metrics_plan.md`:
`Metric_Store.md`.

## The design

* **Canvas** — https://claude.ai/code/artifact/62a2c14d-2d14-488b-ae86-192f8f1ef454
  Open page **App**, then use the **App / KPI** toggle at the top of the
  sidebar. Everything on the page is clickable: the window selector switches
  24 hours / 7 days / 30 days, and every bar and data point has a tooltip.
* **Source** — `design/Main.dc.html`, the same artboard as the customer-facing
  UI. The dashboard is a *mode* of the app, not a separate prototype, so the
  chrome cannot drift.

Everything else — tokens, components, the data layer, the build — is
`Front_End_02/`. This document adds only what is specific to the dashboard.
**Do not restate a token here.**

## Navigation: a toggle, not a nav entry

The KPI dashboard is application-level: it spans every document, so it cannot
sit inside the per-document navigation. It is reached by an **App / KPI
toggle at the top of the sidebar**, two halves side by side under the app
name.

In KPI mode the sidebar drops the document scope — active document, upload,
library, document list — and shows a **System** block instead (api health,
embedder, answer model, worker shape, document count). The Analysis and Chat
tabs disappear for the same reason: they are views of a document, and no
document is in scope.

Route: `/metrics`. This supersedes the "third sidebar entry" wording in
`Front_End_02`, which has been corrected.

## What is on it

Four bands, top to bottom. Deliberately small — this is a first cut.

**1 · Right now** — five tiles, each carrying its threshold in the sub-line
so the number and the bar it must clear are never separated:

| Tile | Sub-line |
|---|---|
| Active now | workers busy · queued |
| Runs | documents · criteria |
| Failure rate | target ≤ 2% |
| p95 job duration | target ≤ 120 s · p50 beside it |
| Spend | per-run cost · daily budget |

**2 · Answer quality** — three meters, each with a bar, a **black tick at the
threshold**, a status chip carrying words, and one line saying what a move
means. These are the three that decide whether the output can be trusted:

| Meter | Threshold | Why |
|---|---|---|
| Quote verification | ≥ 99% | Quotes found verbatim in the passage they cite. The hallucination check |
| Evaluator accept | ≥ 85% | Results passed without a correction round |
| Needs review | ≤ 10% | Results flagged for a human |

**3 · Trend** — two charts over the selected window: runs per hour (bars) and
p50/p95 job duration (line). This is the historical half assignment §3.4 asks for.

**4 · Recent runs** — a table, each row carrying **the trace id its run was
made under**, so any number on this page can be followed into
`.run/app.jsonl`. That link is the reason the table is here at all.

## The initial set

Settled 2026-08-24: eight first-class numbers, all of them plain SQL over
`analyses` today (`Metric_Store.md` phase 1), each with a threshold and an
action:

| KPI | Threshold | When it fires |
|---|---|---|
| Quote verification rate | ≥ 99% | Stop trusting new reports; inspect the failing quotes' criteria — a retrieval or prompt regression |
| Needs-review rate | ≤ 10% | A human looks at the flagged runs; a rising trend means harder contracts or degrading retrieval |
| Mean calibrated confidence | trend only | Watched, not alerted — the seed of the calibration story |
| Failure rate (`failed` + `interrupted`) | ≤ 2% | Never absorbs done-but-`needs_review`, which is a quality error |
| p50/p95 job duration | p95 ≤ 120 s | Percentiles, never the mean; ~60 s parallel measured, headroom on top |
| Cost per run + window spend | ~$0.96/run measured · $50/day budget | Budget breach pauses new runs (`02_costs.md`) |
| Active now | — | Live from `JobRunner`/`/health`, not the table |
| Runs count | — | Denominator and context for every rate above |

**One substitution against the design:** the "Evaluator accept ≥ 85%" meter
cannot be real — its columns are `NULL` until the evaluator lands. Until
then that slot shows **cap rate** (`capped` / `ended_by=cap`), a genuine
quality proxy that swaps out cleanly, and the UI says which one it is
showing. Deferred with their blockers: evaluator accept/revise/fallback
(the evaluator), chat cost/latency and upload timing (`spans`,
`Metric_Store.md` phase 2), per-criterion state mix (phase 3), the per-run
waterfall view.

## Where each number comes from

Nothing on this page needs an endpoint the OpenAPI document does not
already declare — but all four `/metrics/*` operations answer **503** until
`Metric_Store.md` phase 1 lands. The routes, from `05_api_plan.md`:

| Band | Endpoint |
|---|---|
| Tiles, meters | `GET /metrics/summary?window=` — active jobs, runs, failures, p50/p95, cost, quote-verification rate, evaluator accept rate, mean confidence, needs_review rate, cap rate |
| Both charts | `GET /metrics/timeseries?bucket=&window=` — per bucket: runs, p50/p95, cost, mean confidence, state distribution |
| Runs table | `GET /metrics/runs?limit=` |
| *(not designed yet)* | `GET /metrics/runs/{id}/spans` — the per-run waterfall |

The window selector drives `window` and `bucket` together: 24 hours → `1h`
buckets, 7 days → `6h`, 30 days → `1d`.

## Two visualization decisions, made by measurement

Both were checked with a validator rather than by eye, and both changed the
design:

1. **There is no categorical colour anywhere on this page.** Completed/failed
   as green and red bars separates at **ΔE 4.9 for deuteranopia** — the
   classic red-green failure, well below the ΔE 8 floor. So: runs are a
   single hue, p50/p95 are two steps of one hue (lightness differences
   survive every form of colour blindness), and failures live in a tile with
   a chip that carries **words**. Status colour never appears without text.
2. **Chart marks use more chromatic steps than the UI accent.** The oxblood
   `#7A3B2E` has chroma 0.09 and reads gray as a data mark. Charts use
   `#9B4A33` and `#D2907A` — same hue family, on-brand, but they register as
   data. These are chart-only tokens and do not enter the UI palette.

A third, smaller: p50/p95 share one axis because they share a unit. **Never a
second y-axis** — if a future chart wants runs and cost together, that is two
charts.

## Against `KPI_plan.md`

The brainstorm's operational list — per-job cost, tokens, API calls, latency,
upload processing time, success/error counts, aggregates, a 7-day/1-month
trend selector — is right, and bands 1, 3 and 4 implement it.

Where this proposal differs, in one line: **"I don't have much information
regarding accuracy of results" is not true**, and the quality signals are the
half the assignment grades hardest (§3.2 requires confidence calibration and
hallucination detection by name). Four already exist in the data:

* `ResolvedQuote.verified` — every quote is already checked verbatim against
  the passage it cites. Ground-truth-free hallucination detection, and the
  strongest quality signal in the system.
* `ComplianceResult.needs_review`.
* `raw_confidence` (the model's self-estimate) against the derived
  `confidence` and its `confidence_components` — the gap between them over
  many runs *is* a calibration story.
* `structure_rounds` and `ended_by` — how often a correction round was needed,
  and how often the tool-call cap was hit. Both free, both quality proxies.

One more, not a metric: **percentiles, not averages**, for job duration and cost.
The tail is what breaks a demo and the mean hides it.

## Open

1. **Some thresholds still need a measured basis.** Cost is grounded
   (~$0.96 measured per run, `02_costs.md`), job duration is anchored (~60 s
   parallel measured), and the price table behind `cost_usd` was verified
   against published rates on 2026-08-24. The quote-verification 99% and
   needs-review 10% targets are still judgement calls — defensible ones,
   but §3.4 says you will be asked metric by metric.
2. **Calibration is designed nowhere yet.** `raw_confidence` vs `confidence`
   wants a reliability plot, and that is a real chart rather than a tile.
   Worth adding if there is room.
3. **The per-run waterfall** (`/metrics/runs/{id}/spans`) is unbuilt. It is
   the best answer to "walk me through this run" in a live demo, and the most
   expensive thing on this list.
4. **Alert actions are unwritten.** A threshold with no action behind it is a
   number on a screen. Each metric needs: page, ticket, or watch.
5. Figures in the prototype are illustrative and the page says so — the
   database holds one real run.
