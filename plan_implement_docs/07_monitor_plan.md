# Step 14 — the Monitor tab: is the box healthy, not just is the model good

**Status: draft for review, 2026-08-24. Plan only — nothing here is built.**
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
  RED (rate, errors, duration of the **API** surface this process actually
  served) and USE (host RAM and disk) — not Kubernetes-shaped metrics this
  deployment doesn't have. Nginx pointed at the wrong port never reaches this
  process; that stays a Logs / external `/health` story.

## The four metrics

Each is scoped to the **API process only** — this tab has no CLI equivalent,
unlike the KPI tab, because "is the deployment healthy" is not a question
`scripts/analyze.py` running on a laptop can answer. That is a deliberate,
narrower scope than step 13's, and is worth saying explicitly in the doc
rather than leaving a reader to wonder why `spans.surface='cli'` never shows
up here.

### 1. API surface health — request rate, 5xx, p95, this process

**What it is not**: the KPI tab's p95 and failure rate are scoped to analysis
*runs*. This tile is whether **this API process** is answering HTTP. It is
also not the static bundle, not nginx, and not a second copy of the job
inside `spans`.

The full version (every route, five groups, `span("http.request")` into the
KPI table) is dropped. FastAPI's existing trace middleware already wraps
every request for `X-Trace-Id`. After `call_next`, record
`{status, elapsed_ms}` into an in-memory ring if the path is API traffic.

* **Include**: `/api/*` that returns a normal response, and `/health`.
  `POST /api/analyses` stays in — it is supposed to be fast (enqueue); the
  60–180 s job is the KPI tab's problem.
* **Skip**: the static mount (`/`, `/assets/…`); SSE
  (`/api/logs/events`, `/analyses/{id}/events`) — duration is "tab left
  open," not latency. Optional: `POST /api/chat` while it is streaming.
* **Live tile**: last 5 minutes, **this process**: requests/min, 5xx rate,
  p95 of the requests that were not skipped. One number each, not five
  route groups. 4xx is omitted — most of it is a client sending garbage.
* **Historical**: none. A restart zeros the tile, which is honest for "is
  this box healthy."
* **Capture**: `HttpStats` on `app.state`, a few dozen lines on the existing
  `trace` middleware. **No spans, no table.** Polling
  `GET /api/metrics/summary` every 5 s must not land in the same `spans`
  table the waterfall reads.
* **Threshold**: no pager. Chip only, same words-and-colour rule as #4:
  5xx rate > 1% over 5 min → red / "failing"; otherwise green / "answering."
* **What this still cannot see**: nginx upstream on the wrong port. The app
  never saw the request. That remains Logs and an external probe of
  `/health`.

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

* **Live tile**: process RSS and disk **used %** — RSS as a share of host
  RAM (`VmRSS` / `MemTotal` from `/proc`), disk as
  `shutil.disk_usage(DB_PATH.parent)` used/total. Absolute MB and GB sit
  next to the percent so a 90% on a 2 GB box is still readable as a size.
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
  `system_samples(ts, rss_mb, rss_pct, disk_used_pct, uptime_s)`, in the same
  metrics database (still "one file" — the argument `storage.md` already
  makes for `analyses` living beside `documents` applies again: this is
  operational telemetry, not a second datastore).
* **Threshold**: none as an alert. No pager, no `MONITOR_RSS_ALERT_MB`, no
  action that pauses runs. The tile is a **chip**, same rule as the KPI page
  (`thresholds.ts`: colour never carries a fact without words):
  **used &lt; 20% → green / "plenty"**, **used &gt; 90% → red / "tight"**,
  the band in between → warn / "filling". Same scale for disk and for RSS
  as a share of the host. 90% used is the number that matters on this box:
  a full disk fails writes to *both* the contracts DB and the log file at
  once.

## What ties this to the existing metrics infrastructure vs. what's new

| Piece | Reused from step 13 | New |
|---|---|---|
| Storage | same SQLite file, `spans` for #2–#3 | `system_samples` for #4. #1 is in-memory and dies with the process |
| Capture | `span()` for #2 (promote `http.retry` / `http.failed`) and #3 (already emitted) | a few lines on the existing `trace` middleware for #1; a sampler thread for #4 |
| Query layer | `metrics/queries.py` conventions — percentiles in SQL, window parsing | `GROUP BY name` for #3; ring-buffer percentiles in process for #1 |
| API | `routes/metrics.py` pattern | `routes/monitor.py`: `GET /monitor/summary` (live tiles); `GET /monitor/timeseries` only for #2–#4 |
| UI | `MetricsView`'s band layout, `WindowSelector`, `ErrorSurface`-per-band, 5s polling | `MonitorView` at `/monitor`, tab next to `/metrics` |

The reuse is the point: #3 is a query over data that already exists, #2 is
one promotion at a call site that already logs, #1 is a filter on middleware
that already runs. Only #4 has no existing seam.

## Open questions

1. **Sequencing.** This plan assumes the KPI page, Logs tab, `metrics`
   package and `spans` table are already on the branch — they are. Building
   Monitor is not blocked on a merge that has already happened.

## Cut order if over budget

`system_samples` sampler thread (#4) first — it is the only piece with no
existing seam, so it is the most expensive relative to what it adds →
pipeline stage map (#3) last, since it's a query over data that will already
exist the moment step 13 lands, regardless of anything in this plan.
