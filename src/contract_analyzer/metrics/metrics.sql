-- Applied by the metrics store, on the same connection and the same database
-- file as schema.sql. Two files, one store.
--
-- The line between them: **schema.sql is what happened** -- documents, chunks,
-- analyses, the domain objects a client comes back for -- and **metrics.sql is
-- how it went**. The API's storage must not depend on the metrics module to
-- record a report, so `analyses` is over there and `spans` is here.
--
-- It is a second *file* for a mechanical reason as well. db.py runs schema.sql
-- through str.format(dim=...) to fix the vector width, so every literal brace
-- in that file -- in a comment included -- is a format placeholder. Span DDL
-- reads `json_extract(attrs, '$.model')`; it cannot live there.
--
-- CREATE TABLE IF NOT EXISTS throughout, so a database built before this
-- existed just grows the tables the first time a store opens it.

-- One row per `span.end` log record, written by the handler in handler.py.
-- Not sampled: one analysis is ~70 rows and a thousand analyses are a few
-- megabytes, while a sampling knob nobody tunes is a knob set wrong during a
-- demo.
--
-- **No foreign keys, deliberately.** DELETE /documents/{id} must not take the
-- KPI history with it -- history that vanishes when somebody tidies up the
-- corpus is not history -- and a span whose run has been pruned is still a
-- true record of a call that was made. The same argument as `analyses`, and
-- tests/test_metrics.py asserts the outcome rather than trusting nobody adds
-- the constraint later.
CREATE TABLE IF NOT EXISTS spans (
    span_id        TEXT PRIMARY KEY,
    -- What makes the waterfall a tree rather than a list.
    parent_span_id TEXT,
    -- The id in .run/app.jsonl: the join from any number on the KPI page back
    -- to the lines that produced it.
    trace_id       TEXT,
    -- The analysis this span belongs to, from the run_id context variable.
    -- NULL for everything that is not part of a run -- ingestion, and chat,
    -- which is stateless by design and is queried as `name = 'chat'`.
    run_id         TEXT,
    -- 'agent.call', 'analysis.criterion', 'retrieve', 'chat', 'ingest.embed'...
    name           TEXT NOT NULL,
    -- 'ok' or 'error', as the span context manager recorded it.
    status         TEXT,
    latency_ms     REAL,
    -- UTC ISO-8601 with an explicit offset, the same spelling analyses uses,
    -- so a window bound is a string compare rather than a per-row datetime().
    ts             TEXT NOT NULL,

    -- Promoted out of `attrs` because every KPI query touches them and
    -- json_extract on a million rows to group by model is a table scan with
    -- extra steps. Each is NULL on the spans that do not carry it.
    surface        TEXT,
    criterion      TEXT,
    document_id    INTEGER,
    model          TEXT,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    cost_usd       REAL,

    -- Everything else the span bag held, as JSON. The waterfall shows it, and
    -- a question nobody anticipated is one json_extract away instead of one
    -- migration away.
    attrs          TEXT
);

CREATE INDEX IF NOT EXISTS idx_spans_run ON spans (run_id);
CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans (trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_name_ts ON spans (name, ts);

-- One row per criterion per run: the grain between `analyses` (one row per
-- run) and `spans` (one row per step). Written by `finish_analysis` from the
-- report it is already holding, and backfillable from `report_json` with
-- json_each, so it can land late without losing history.
--
-- What it is for is the question `report_json` answers badly: **the same file
-- hash coming back with a different compliance state is drift**, and finding
-- that in a blob means mining every report on every query. It is also the raw
-- material of the calibration story -- `raw_confidence`, the model's own
-- estimate, against the derived `confidence`, per criterion, over many runs.
--
-- Not on the dashboard's first cut. Queryable is enough.
--
-- The primary key is (run_id, criterion_id): a criterion is analysed once per
-- run, and re-running one is a new run. No foreign key, for the same reason
-- `spans` has none.
CREATE TABLE IF NOT EXISTS criterion_results (
    run_id           TEXT    NOT NULL,
    criterion_id     TEXT    NOT NULL,
    -- 'Fully Compliant' | 'Partially Compliant' | 'Non-Compliant'.
    state            TEXT,
    -- The derived confidence, already cut by the verify ratio and by missing
    -- sub-requirements, and the model's own raw estimate beside it. The gap
    -- between the two over many runs is the calibration story.
    confidence       REAL,
    raw_confidence   REAL,
    needs_review     INTEGER,
    -- 'stop' when the model finished, 'cap' when a counter stopped it.
    ended_by         TEXT,
    structure_rounds INTEGER,
    tool_calls       INTEGER,
    cost_usd         REAL,
    quotes_total     INTEGER,
    quotes_verified  INTEGER,
    latency_s        REAL,
    -- Declared now, NULL until the evaluator lands. The same argument as the
    -- evaluator_* trio on `analyses`: declaring a column costs nothing and
    -- removes an ALTER TABLE from the middle of a later step.
    evaluator_verdict TEXT,
    PRIMARY KEY (run_id, criterion_id)
);

CREATE INDEX IF NOT EXISTS idx_criterion_results_criterion
    ON criterion_results (criterion_id);

-- One row per sampler tick (~30s), written by sampler.py from the API process.
-- Not a span: this is the box, not a step. HTTP columns stay NULL until the
-- in-memory request ring lands; host queries ignore them. Same file, no
-- second database.
--
-- ts is the primary key. A tick that collides on the second replaces the
-- previous one; 30 s apart never collides, and a test that ticks faster still
-- has a row.
CREATE TABLE IF NOT EXISTS system_samples (
    ts             TEXT PRIMARY KEY,
    -- VmRSS of this process, and that as a share of MemTotal. ru_maxrss is a
    -- high-water mark and would only ever go up.
    rss_mb         REAL,
    rss_pct        REAL,
    -- shutil.disk_usage of the database directory: used / total, plus the
    -- sizes the tile prints so 90% on a 2 GB box is still a size.
    disk_used_pct  REAL,
    disk_used_gb   REAL,
    disk_total_gb  REAL,
    http_rpm       REAL,
    http_5xx_rate  REAL,
    http_p95_ms    REAL
);
