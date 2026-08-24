# Step 14 — the Monitor tab: is the box healthy, not just is the model good

**Status: draft for review, 2026-08-24. Plan only — nothing here is built.**
Blocked by step 13 (`06_metrics_plan.md`, the metrics store) landing on
`main` — it is on `origin/KPI`/`origin/LOGS` today, unmerged. Blocks nothing.

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
  RED (rate, errors, duration of the *whole* HTTP surface) and USE
  (utilization/saturation/errors of the *host*) — not Kubernetes-shaped
  metrics this deployment doesn't have.

## The four metrics (a fifth, cut first if short on time)

Each is scoped to the **API process only** — this tab has no CLI equivalent,
unlike the KPI tab, because "is the deployment healthy" is not a question
`scripts/analyze.py` running on a laptop can answer. That is a deliberate,
narrower scope than step 13's, and is worth saying explicitly in the doc
rather than leaving a reader to wonder why `spans.surface='cli'` never shows
up here.

### 1. API surface health — request rate, error rate, latency, *every* route

**What it is not**: the KPI tab's p95 latency and failure rate are scoped to
`runs` — the LLM pipeline. A missing static bundle
(`[[streamlit-obsolete]]`'s exact failure mode: `start.bash` reports success,
`/health` is green, `/` serves nothing) never touches an analysis run and
would never move a single KPI number. This metric is the whole HTTP surface —
`/health`, `POST /documents`, `GET /documents`, `POST /analyses`, `POST
/chat`, `POST /documents/{id}/search`, and `/` itself — broken out by route
group.

* **Live tile**: requests/min, error rate (%4xx, %5xx) over the last 5
  minutes, p95 latency, each split by route group (`ingest`, `analysis`,
  `chat/search`, `static/ui`, `health`).
* **Historical**: the same, bucketed over the page's window selector
  (reuses `WindowSelector` — 1h/24h/7d), one line per route group so a
  regression in one group doesn't hide inside an average.
* **Capture**: a FastAPI middleware wrapping every request in the existing
  `span("http.request", route=..., method=..., status_code=...)` — this is
  the same seam `06_metrics_plan.md` used for the LLM pipeline
  ("the span() seam is where the KPI store will subscribe; nothing in the
  call sites will change"), applied one layer further out. **Zero new
  tables** — it lands in `spans` exactly like `agent.call` does, and the
  query is `WHERE name = 'http.request'` grouped by `route`.
* **Threshold**: page on 5xx rate > 1% over 5 min (an app that answers 500s is
  broken regardless of what the model thinks); watch, don't page, on 4xx —
  most 4xx here is a client sending garbage, not the deployment failing.
  p95 latency alert scoped to the *non-analysis* routes only (`health`,
  `ingest`, `chat/search` list calls) at something like 2 s — `POST
  /analyses` itself is fire-and-forget and its 60–180 s run time is the KPI
  tab's problem, not this one's.

### 2. Upstream dependency reliability — is it us or is it Anthropic/OpenAI

**What it is not**: cost per token, cost per run — KPI tab, explicitly out of
scope here per your instruction. This is *reliability*, not spend: retry
rate and exhausted-retry rate for calls through `http_client.py`, broken down
by reason (`docs/http-client.md`'s own taxonomy — connect/read/write/pool
timeout, connection error, protocol error, 408/409/429/500/502/503/504).

* **Live tile**: retries per 100 upstream calls (last 5 min), exhausted-retry
  (total failure) rate, and the top failure reason right now.
* **Historical**: retry rate over the window, one line per reason, so a
  demo-time incident is legible as "429s spiking" vs. "connect timeouts
  spiking" — the first says *we're being rate-limited*, the second says
  *network path is bad* — different responses, same chart today shows neither.
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

### 3. Pipeline stage failure map — *where*, not *whether*

**What it is not**: KPI's `runs.status` failure rate is one number for an
entire five-criterion analysis. A single `ingest.embed` failure and a single
`agent.call` failure both just show up as "this run failed" — indistinguishable
from the dashboard, distinguishable only by opening the Logs tab and reading.

* **Live tile**: the worst-performing span name right now (highest error
  rate over the last 5 min, minimum sample size), e.g. "`ingest.parse`: 3 of
  8 failed (37%)".
* **Historical**: a small table or heatmap, one row per span name
  (`ingest.parse`, `ingest.embed`, `retrieve`, `agent.tool`, `agent.call`,
  `chat`), columns = error rate and p95 latency over the window. Reads like a
  service-dependency table because that's what a span name already is here.
* **Capture**: **nothing new.** This is a pure query over the `spans` table
  step 13 already builds — `GROUP BY name` instead of `GROUP BY run_id`. It
  is the cheapest of the four and ships the same day the spans table exists,
  independent of the other three.
* **Threshold**: page if any single stage's error rate > 5% over the window
  with at least ~10 samples (the "at least N samples" guard matters more
  here than anywhere else on this tab — `retrieve` might see one call in a
  quiet hour, and 1-of-1 failed is not a 100% error rate worth paging on).
* **Why this earns its place**: it's the one that turns "the demo broke"
  into "the demo broke *in parsing*" without anyone tailing `.run/app.jsonl`
  live — which §3.6 asks for as a *capability*, and this is the dashboard
  form of it.

### 4. Host resource headroom — the actual failure mode on one VPS

**What it is not**: pod evictions, autoscaling, node pressure — none of
which exist here. `[[demo-deployment]]` is one uvicorn process on one host
with no firewall but nginx, one SQLite file that only grows, and one JSON log
file (`.run/app.jsonl`) that also only grows, since step 13 explicitly ships
with no retention policy ("There is no retention policy and none is planned
until asked for"). On this box, **disk fills up or the process gets OOM-killed
long before any model-quality metric would tell you something is wrong.**

* **Live tile**: process RSS memory, free disk % on the volume holding
  `data/`, process uptime.
* **Historical**: RSS and disk-free % over the window — the two curves that
  matter are their *slope*, not their instantaneous value (a slow SQLite/log
  file creep is invisible on a live tile and obvious on a 7-day chart), plus
  an uptime chart that reads as a sawtooth — every restart is a visible drop
  to zero, which is itself a signal (an unexpected restart is a deploy
  nobody ran, and `[[demo-deployment]]` has already shown its `start.bash`
  can silently half-fail).
* **Capture**: **the one genuinely new piece.** A daemon sampler thread,
  started from the API's lifespan next to `reconcile()`, ticking every ~30s:
  `resource.getrusage(RUSAGE_SELF).ru_maxrss` (or `/proc/self/status`
  `VmRSS`, more accurate — `ru_maxrss` is a high-water mark, not current) for
  memory, `shutil.disk_usage(DB_PATH.parent)` for disk — both stdlib, no new
  dependency, matching this project's existing allergy to adding a library
  for something the standard library already does. Writes to one new table,
  `system_samples(ts, rss_mb, disk_free_pct, uptime_s)`, in the same
  metrics database (still "one file" — the argument `storage.md` already
  makes for `analyses` living beside `documents` applies again: this is
  operational telemetry, not a second datastore).
* **Threshold**: alert disk free < 15%, and separately < 5% as a hard page
  (a full disk fails writes to *both* the contracts DB and the log file at
  once — a double failure, not two independent ones); memory threshold is a
  config value (`MONITOR_RSS_ALERT_MB`, default left as an open question
  below — it depends on the box's actual RAM, which this plan doesn't know)
  rather than a hardcoded percentage, unlike the others.

### 5. (bonus, cut first) Long-lived connection count

The Logs tab (`origin/LOGS`) and the analysis progress view both hold open
SSE connections (`/api/logs/events`, `/analyses/{id}/events`). A demo browser
tab left open on the Logs view, or a client that reconnects without closing
the old stream, accumulates open connections on a single-process server —
a leak this app's own design can cause that generic infra monitoring
wouldn't flag. Live tile: open SSE connections right now, by endpoint;
historical: the same, on the sampler's 30s tick (same `system_samples` row,
one more column — no third table). Threshold: watch-only, no clear number
without a load test to anchor it. **Cut first** if the other four run over
budget: it depends on the Logs tab's own connection-tracking already
existing (it may not), and it is the one whose absence a panel is least
likely to notice.

## What ties this to the existing metrics infrastructure vs. what's new

| Piece | Reused from step 13 | New |
|---|---|---|
| Storage | same SQLite file, same `spans` table for #1–#3 | one table, `system_samples`, for #4 (and #5's extra column) |
| Capture mechanism | `span()` context manager — metrics #1–#3 are *more calls to a seam that already exists*, at the HTTP-middleware and http-client layers | a daemon sampler thread for #4/#5, the one piece with no existing seam to hook |
| Query layer | `metrics/queries.py` conventions — percentiles in SQL, window parsing, `summary()`/`timeseries()` shape | new functions in the same module (or a sibling `monitor_queries.py` if `metrics/queries.py` gets unwieldy — decide at build time), `GROUP BY route`/`name` instead of `GROUP BY run_id` |
| API | `routes/metrics.py` pattern | `routes/monitor.py`: `GET /monitor/summary`, `GET /monitor/timeseries` |
| UI | `MetricsView`'s band layout, `WindowSelector`, `MetricTile`, `ErrorSurface`-per-band, 5s polling, `charts.ts` | `MonitorView` at route `/monitor`, tab placed next to `/metrics` in the same nav toggle (`ModeToggle`/`Segmented`, whichever `origin/KPI` lands with) |

The reuse is the point: three of the four metrics need no new instrumentation
call sites beyond the one middleware and the one promoted log-to-span point —
the same "eight modules change by zero lines" argument step 13 made for the
KPI tab holds again here, one layer further from the model.

## Open questions

1. **Sequencing.** This plan assumes `origin/KPI` (which includes
   `origin/LOGS`) merges to `main` first — `MetricTile`, `WindowSelector`,
   `charts.ts`, the `metrics` package and its `spans` table all come from
   there and don't exist on `main` today. Building Monitor before that merges
   means building on a branch that itself isn't reviewed yet.
2. **Memory threshold.** `system_samples.rss_mb` is easy to capture; the
   alert threshold depends on the VPS's actual RAM, which isn't recorded
   anywhere in the repo or `[[demo-deployment]]`. Needs one number from
   `free -h` on the host before `thresholds.ts` can carry it honestly, the
   same way the cost target in step 13 needed one real measured run before
   the threshold could be trusted.
3. **Middleware placement.** FastAPI middleware wraps `/` (the static UI
   bundle) too — hundreds of asset requests per page load would dominate the
   `http.request` span volume and possibly the "requests/min" tile with
   noise that isn't API traffic. Likely answer: route-group the static
   mount separately and give it its own (probably ignored) bucket rather than
   folding it into the same rate number as `POST /analyses` — decide at
   build time, flag here so it isn't a surprise mid-implementation.
4. **Does #5 ship at all.** Depends on whether the Logs tab already tracks
   open-connection count anywhere (`useLogs.ts`/the SSE fan-out on the
   backend) — if it does, #5 is nearly free; if not, it's a second new
   piece of state to track and probably not worth it for a bonus tile.

## Cut order if over budget

`system_samples` sampler thread (#4) and #5 first — they're the only pieces
with no existing seam, so they're the most expensive relative to what they
add → pipeline stage map (#3) last, since it's a query over data that will
already exist the moment step 13 lands, regardless of anything in this plan.
