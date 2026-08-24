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
