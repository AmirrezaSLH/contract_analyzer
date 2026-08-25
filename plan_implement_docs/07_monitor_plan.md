# Step 14 — the Monitor tab: is the box healthy, not just is the model good

**Status: host, stages, and upstream are built. HTTP request stats are out of scope.**
related to `06_metrics_plan.md`

**The one-sentence version.** The KPI tab (`/metrics`) answers *"is the
analysis any good and what did it cost"* — model quality and spend, scoped to
`runs`. The Monitor tab (`/monitor`) answers *"is the deployment itself
healthy"* — the RED/USE signals of a single always-on process on one VPS,
deliberately with zero dollar signs on it.

## Why a second tab and not five more tiles on the first

`MetricsView`'s own doc comment already draws this line: "cost runs through
three [bands] rather than sitting in one... never eight cost tiles." The same
discipline argues for a second page here, not scope creep on the first:

* **Different audience, same person.** The KPI tab is what you show to
  justify the model choice and the accuracy. The Monitor tab is what you'd
  actually open at 3 a.m. when `demo.amirrezaslh.com` is down and the KPI
  numbers are irrelevant because no requests are completing at all.
* **Different failure domain.** Every KPI number today is *derived from a
  finished `AnalysisReport`* — a run that failed to complete cleanly still
  produces a row (`fail_run`, `interrupted`). A box with a full disk, an OOM'd
  process, or an nginx pointed at the wrong port produces **no row at all**,
  and `[[demo-deployment]]`'s own incident (nginx upstream stuck on the dead
  Streamlit port after `[[streamlit-obsolete]]`) is exactly that: the app's
  own logs showed nothing wrong, because the app never saw the request.
* **User's instruction for this pass**: cost is out of scope here — it is
  §3.4's KPI tab's job. This tab is §3.5's "other signals," and the honest
  reading of §3.5 for a system that is one process on one VPS behind nginx is
  USE (host RAM and disk) plus where a pipeline span failed — not Kubernetes-
  shaped metrics this deployment doesn't have, and not a second copy of HTTP
  request stats. Nginx pointed at the wrong port never reaches this process;
  that stays a Logs / external `/health` story.

## The three metrics

Each is scoped to the **API process only** — this tab has no CLI equivalent,
unlike the KPI tab, because "is the deployment healthy" is not a question
`scripts/analyze.py` running on a laptop can answer. That is a deliberate,
narrower scope than step 13's, and is worth saying explicitly in the doc
rather than leaving a reader to wonder why `spans.surface='cli'` never shows
up here.

**HTTP request rate / 5xx / p95 is not on this tab.** It was sketched as
metric #1 and then cut: nginx never-seen traffic would still be invisible,
KPI already has run latency and failure rate, and the remaining three
already answer "is the box dying" and "where did the pipeline break."

### 1. Upstream dependency reliability — is it us or is it Anthropic/OpenAI

**What it is not**: cost per token, cost per run — KPI tab, explicitly out of
scope here per your instruction. This is *reliability*, not spend: retry
rate and exhausted-retry rate for calls through `http_client.py`, broken down
by reason (`docs/http-client.md`'s own taxonomy — connect/read/write/pool
timeout, connection error, protocol error, 408/409/429/500/502/503/504).

* **Live tiles**: retries per 100 upstream calls (last 5 min), exhausted-retry
  (total failure) rate, and the top failure reason right now.
* **Historical**: retry rate and exhausted rate over the window — two line
  charts, one unit each. A breakdown by reason is a tooltip / the "top
  reason" tile, not five lines on one axis.
* **Capture**: `RetryingTransport` already logs `http.retry` (WARNING) and
  `http.failed` (ERROR) with method, URL, attempt, reason — right now those
  are log lines nobody aggregates. Promote them to zero-duration spans
  (`span("upstream.retry", reason=..., attempt=...)` /
  `span("upstream.failed", reason=..., status=...)`) at the same call site
  that already logs them. Same `spans` table, no new table, no call-site
  churn anywhere else — the exact shape of decision 1 in `06_metrics_plan.md`.
* **Threshold**: alert on exhausted-retry rate > 1% of upstream calls over 5
  min (every one of those is a criterion or a chat turn the user sees fail);
  watch retry rate > 10% as an early warning that precedes it.
* **Why this earns its place over more KPI-tab tiles**: it is the single
  fastest way to answer "is this on us" in the middle of a live demo, and
  nothing on the KPI tab distinguishes "the model said something wrong" from
  "Anthropic 503'd twice and we retried our way past it."

### 2. Pipeline stage failure map — *where*, not *whether*

**What it is not**: KPI's `runs.status` failure rate is one number for an
entire five-criterion analysis. A single `ingest.embed` failure and a single
`agent.call` failure both just show up as "this run failed" — indistinguishable
from the dashboard, distinguishable only by opening the Logs tab and reading.

* **Live tiles**: the worst-performing span name right now (highest error
  rate over the last 5 min, minimum sample size), plus its error rate and
  the total error count across the named stages, e.g. "`ingest.parse`" /
  `37%` / `12`. One extra tile for sample count, so a 1-of-1 is visible as
  "not enough" rather than 100%.
* **Historical**: line charts of error rate for that worst name, and of
  total error count across the short list (`ingest.parse`, `ingest.embed`,
  `retrieve`, `agent.tool`, `agent.call`, `chat`) over the window. Not a
  heatmap — the page is tiles then lines, same as the other three sections.
* **Capture**: **nothing new.** This is a pure query over the `spans` table
  step 13 already builds — `GROUP BY name` instead of `GROUP BY run_id`. It
  is the cheapest of the three and ships the same day the spans table exists,
  independent of the other two.
* **Threshold**: page if any single stage's error rate > 5% over the window
  with at least ~10 samples (the "at least N samples" guard matters more
  here than anywhere else on this tab — `retrieve` might see one call in a
  quiet hour, and 1-of-1 failed is not a 100% error rate worth paging on).
* **Why this earns its place**: it's the one that turns "the demo broke"
  into "the demo broke *in parsing*" without anyone tailing `.run/app.jsonl`
  live — which §3.6 asks for as a *capability*, and this is the dashboard
  form of it.

### 3. Host resource headroom — the actual failure mode on one VPS

**What it is not**: pod evictions, autoscaling, node pressure — none of
which exist here. `[[demo-deployment]]` is one uvicorn process on one host
with no firewall but nginx, one SQLite file that only grows, and one JSON log
file (`.run/app.jsonl`) that also only grows, since step 13 explicitly ships
with no retention policy ("There is no retention policy and none is planned
until asked for"). On this box, **disk fills up or the process gets OOM-killed
long before any model-quality metric would tell you something is wrong.**

* **Live tiles**: process **memory** used % and disk used %, each with MB/GB
  in the sub-line so a 90% on a 2 GB box is still a size. The tile is
  labelled Memory, not RSS — that is the `/proc` name, not the interview
  name. "How much RAM this process is using" is the sentence.
* **Historical**: memory % and disk used % over the window — slope, not the
  live snapshot. Two charts, one unit (percent) that they share, but still
  two cards so a disk cliff is not scaled against a flat memory line. No
  uptime sawtooth on this pass.
* **Capture**: **the one genuinely new piece.** A daemon sampler thread,
  started from the API's lifespan next to `reconcile()`, ticking every ~30s:
  `resource.getrusage(RUSAGE_SELF).ru_maxrss` (or `/proc/self/status`
  `VmRSS`, more accurate — `ru_maxrss` is a high-water mark, not current) for
  memory, `shutil.disk_usage(DB_PATH.parent)` for disk — both stdlib, no new
  dependency, matching this project's existing allergy to adding a library
  for something the standard library already does. Writes to one new table,
  `system_samples(ts, rss_mb, rss_pct, disk_used_pct, disk_used_gb,
  disk_total_gb)`, in the same metrics database (still "one file"). Host
  charts bucket at `monitor_sample_seconds`. Unused HTTP columns that landed
  on an earlier draft of this table are not filled and are not shown.
* **Threshold**: none as an alert. No pager, no `MONITOR_RSS_ALERT_MB`, no
  action that pauses runs. The tile is a **chip**, same rule as the KPI page
  (`thresholds.ts`: colour never carries a fact without words):
  **used &lt; 20% → green / "plenty"**, **used &gt; 90% → red / "tight"**,
  the band in between → warn / "filling". Same scale for disk and for
  process memory as a share of the host. 90% used is the number that matters
  on this box:
  a full disk fails writes to *both* the contracts DB and the log file at
  once.

## Front end

Three sections on one page, same skeleton in each: **a row of labelled tiles,
then line charts of those numbers over the window.** No heatmap, no extra
bands, no cost, no HTTP RED. The KPI page already has the parts; this is
composition.

`MonitorView` at `/monitor`. Application-level, like `/metrics` and `/logs`:
no `:id`, no document tabs, sidebar drops the library block. `ModeToggle`
gains a fourth link (App / KPI / Monitor / Log). The track already uses
`flex: 1` per option; four labels are tighter than three and that is fine.
`App.tsx` treats `/monitor` as `offApp` the same way it treats KPI and Log.

Reuse, do not fork:

| Piece | Where |
|---|---|
| Tiles | `MetricTile` — label, value, optional chip, sub-line |
| Charts | `charts.ts` `lineGeometry` + the SVG in `TrendBand` (copy the card, do not import KPI types into Monitor) |
| Window | Monitor's own selector — 30 min / 1 h. Host charts bucket at `monitor_sample_seconds`. Stages charts are 1-minute bars. KPI keeps 24h / 7d / 30d. |
| Errors | `ErrorSurface` per section, so one failed query does not blank the page |
| Poll | 5 s on `GET /monitor/stages`, `/monitor/host`, and `/monitor/upstream`, same as KPI |

**Chips still carry words.** Colour never travels alone (`thresholds.ts` on
KPI). Host: used &lt; 20% → green / "plenty", &gt; 90% → red / "tight", else
warn / "filling". Upstream exhausted &gt; 1% and stage error &gt; 5% (n ≥ 10)
use the same chip pattern; they are not pagers.

**Chart rules, copied from `charts.ts`:** null percentiles **break the
line**, they are not drawn as 0. Empty buckets are allowed on the axis.
**One unit per chart.** The last bucket is the current partial one.

| Section | Tiles | Charts |
|---|---|---|
| Upstream | retries/100, exhausted rate, top reason | retry rate, exhausted rate from `spans` |
| Stages | worst name, its error rate, total errors, n | error rate for that name, error count across the short list from `spans` |
| Host | Memory %, disk % | those two series from `system_samples` |

`GET /monitor/stages`, `GET /monitor/host`, and `GET /monitor/upstream` are
the live payloads. The top-reason tile is titled **Top HTTP status** or
**Top error type** (ConnectError, ReadTimeout, …) and shows that reason's
share of retry + exhausted events.

## What ties this to the existing metrics infrastructure vs. what's new

| Piece | Reused from step 13 | New |
|---|---|---|
| Storage | same SQLite file, `spans` for upstream and stages | `system_samples` for host |
| Capture | `span()` for upstream and stages | sampler thread writes host snapshots |
| Query layer | percentiles in SQL, window parsing | `GROUP BY name` for stages; `system_samples` for host history |
| API | `routes/metrics.py` pattern | `routes/monitor.py`: `GET /monitor/stages`, `GET /monitor/host`, `GET /monitor/upstream` |
| UI | `MetricTile`, `WindowSelector`, `lineGeometry`, `ErrorSurface`, 5s polling | `MonitorView`, fourth `ModeToggle` link |

The reuse is the point: stages is a query over data that already exists,
upstream is one promotion at a call site that already logs. Only host had no
existing seam. HTTP request stats were cut rather than sharing the sampler.

## Open questions

1. **Sequencing.** This plan assumes the KPI page, Logs tab, `metrics`
   package and `spans` table are already on the branch — they are. Building
   Monitor is not blocked on a merge that has already happened.

## Cut order if over budget

`system_samples` sampler thread (host) first — it is the only piece with no
existing seam, so it is the most expensive relative to what it adds →
pipeline stage map last, since it's a query over data that will already
exist the moment step 13 lands, regardless of anything in this plan.
