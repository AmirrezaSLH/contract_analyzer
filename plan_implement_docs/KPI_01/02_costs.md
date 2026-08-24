# KPI 01 · cost metrics

**Status: proposed, 2026-08-24.** The cost slice of the KPI page: which cost
numbers to track, where each one comes from today, and what has to change
before the rest become queryable. Grain and storage rules are
`01_findings.md`; this document applies them to cost only.

The wishlist this answers: cost per job, tokens used, total cost, average
cost, historical trend, cost per model, cost per surface (chat vs analyze),
and embedding cost.

## 1. The one rule

**Dollars on the tile, tokens and calls behind it.** Cost is the only family
where three numbers describe the same event — API calls, tokens, dollars —
and promoting all three to tiles says the same thing thrice. The dollar is
what a budget is written in; tokens explain the dollar; the call count
explains retries and caps. Store all three, display one.

And per `01_findings.md`: **percentiles and totals, not averages, wherever
the number can alarm.** An average cost per run is fine as context on the
Spend tile; the number that should page someone is the window total against
the budget, and the p95 that says one run went wild.

## 2. What each wanted number is, and where it lives

Three tiers, by what it takes to get the number.

### Tier 1 — SQL over `analyses`, today

Nothing below needs a schema change or a new write path. These are the
numbers `/metrics/summary` and `/metrics/timeseries` should serve first.

| Wanted | Query | Notes |
|---|---|---|
| Cost per analysis | `cost_usd` per row | Already derived on completion from `report.totals` |
| Tokens per analysis | `input_tokens`, `output_tokens` | Same |
| Total cost (window) | `SUM(cost_usd) WHERE created_at >= …` | The Spend tile's headline |
| Average cost | `AVG(cost_usd)`, plus `p50/p95` | Show p50 next to the mean; the tail is the story |
| Historical trend | `SUM(cost_usd)` per bucket via `timeseries?bucket=` | 24h→1h, 7d→6h, 30d→1d, same as every other chart |
| Analyze split by caller | `GROUP BY surface` (`ui`/`cli`/`mcp`/`api`) | Free slice; not the same thing as chat-vs-analyze |

Measured anchor: a five-criterion run on the sample contract is **~$0.96**
(`00_README.md`, and the `<$0.40` target in the overall plan is stale
against it — re-target or publish the cost/quality curve, per
`01_findings.md` §1).

### Tier 2 — recorded but not queryable: chat and per-model

Both numbers are *computed* on every call and then dropped on the floor as
far as SQL is concerned.

**Cost per chat turn.** Chat is stateless by design: the API returns
`AnswerResult` (which carries `cost_usd`, usage, model) and logs a `chat`
span to `.run/app.jsonl`. No row is written anywhere. Until the `spans`
table from `06_metrics_plan.md` lands and ingests `span.end` records, chat
cost exists only in the log. **Do not** reconstruct it from React session
state and do not give chat a row in `analyses` — the settled design
(`01_findings.md` §6) is that chat becomes queryable as
`spans WHERE name = 'chat'`.

**Cost per model.** `analyses` has **no model column.** The model is
recorded in two places, neither first-class:

* every `agent.call` span in `.run/app.jsonl` carries `model`, tokens and
  `cost_usd` — the true per-call, per-model record, covering chat and
  analysis alike;
* `report_json` carries the model per criterion result, minable with
  `json_each` the way the documents list already mines `last_analysis`.

So per-model cost has two honest implementations: mine `report_json` (works
today, analysis only, ugly), or wait for `spans` and do
`SELECT json_extract(attrs,'$.model'), SUM(cost) … GROUP BY 1` over
`agent.call` spans (covers everything, and is what `metrics.sql` is for).
**Recommendation: the latter.** A one-model deployment makes the
`report_json` mining a lot of work for a chart with one bar; the moment a
second model appears (a cheaper chat model, an A/B), `spans` pays for both
this and chat cost at once.

Pricing itself is already centralised in `generation/pricing.py` — USD per
million tokens per model, cache reads at 0.1×, writes at 1.25×, unknown
models price at $0.00 and log `pricing.unknown_model` once. A `$0.00`
average cost on the tile therefore has a known failure meaning: **a model id
the price table has not learned**, not a free run. Worth one line on the
dashboard's footnote.

### Tier 3 — not computed anywhere: embedding cost

Ingestion logs an `ingest.embed` span with the chunk count and embedder
name, but **no token count and no dollar figure** — the embeddings response
usage is never read, and `pricing.py` prices only generation models.

Scale check before building anything: at $0.02/1M tokens, embedding the
21-page sample (~10k tokens of chunk text) costs **about $0.0002** — four
orders of magnitude under the ~$0.96 analysis it enables. Embedding cost
will never move a budget here.

**Recommendation: capture it, do not tile it.** The capture is small and
honest — read `usage.total_tokens` off the embeddings API response in
`embeddings/openai.py`, add the embedding rate to `pricing.py`, and put
`tokens` and `cost_usd` on the `ingest.embed` span bag. It then arrives in
the store for free when `spans` lands, appears in the per-run waterfall, and
supports the sentence the panel will actually ask for: *"ingestion costs a
fiftieth of a cent; the dollar is all reasoning."* The local embedder prices
at $0.00 truthfully. A dedicated embedding tile would be a number with four
leading zeros.

## 3. What the dashboard shows

Cost gets **one tile, one chart, one breakdown** — not eight tiles.

1. **Spend tile** (exists in the design): window total headline, `per-run
   p50 · daily budget` sub-line. Alert action: budget breach pauses new runs
   and triggers the re-target conversation, not a silent overrun.
2. **Cost trend** (add to band 3): `SUM(cost_usd)` per bucket as bars, same
   single-hue treatment as runs-per-hour. Per the design rule it shares no
   axis with anything — runs and cost together is two charts.
3. **Breakdown, drill-down not tile**: cost share by agent/model as one
   stacked chart once `spans` exists, and per-run cost already sits in the
   runs table row by row. Tokens appear in the run drill-down and the
   waterfall, not on the page.

Thresholds to defend: **daily budget** (a real number, set from expected
demo volume — 18 runs ≈ $17, so $50/day is the design's placeholder and a
defensible starting point) and **p95 cost per run ≤ ~2× p50** as the
"one run went wild" tripwire. The average carries no threshold; it is
context.

## 4. Order of work

1. **Now:** `summary` + `timeseries` cost fields from `analyses` (total,
   avg, p50/p95, per-bucket trend, `surface` split). No schema change.
2. **With `spans` (the still-live half of `06_metrics_plan.md`):** chat cost
   per turn, cost per model, cost share by agent — all queries over
   `agent.call` / `chat` spans, no new columns on `analyses`.
3. **Small code change, any time:** embedding usage onto the `ingest.embed`
   span + embedding rate in `pricing.py`. Ten lines, and the waterfall
   sentence comes free.

What this deliberately does not do: add a model column to `analyses`
(the spans query answers it better and covers chat), give chat a run row,
or build an `analytics` table — all settled in `01_findings.md` §4.
