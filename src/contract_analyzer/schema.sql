-- Applied by db.py, which substitutes {dim} with the active embedder's vector
-- width before executing. This file is a template, not valid SQL on its own:
-- a vec0 table's width is fixed at creation, so it must come from config.
--
-- Four tables carry the corpus:
--   documents   one row per ingested file
--   chunks      the text, and everything a citation needs (source, page,
--               printed page label, section breadcrumb, element type)
--   chunks_vec  vectors, queried by KNN                        (sqlite-vec)
--   chunks_fts  the same text, queried by BM25                 (FTS5)
-- chunks is the single source of truth; the other two are indexes over it.
--
-- A fifth table, `analyses`, records the work done *on* a contract -- see the
-- comment above it, at the bottom of this file.

CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY,
    path          TEXT    NOT NULL UNIQUE,
    filename      TEXT    NOT NULL,
    -- Hash of the file's bytes: re-ingesting an unchanged file is a no-op.
    content_hash  TEXT    NOT NULL,
    page_count    INTEGER,
    -- Diagnostics: which tool produced the PDF, and whether it carried an
    -- outline. A file with no outline had its sections inferred, which is
    -- worth knowing when a citation looks wrong.
    producer      TEXT,
    has_outline   INTEGER NOT NULL DEFAULT 0,
    -- Where the section breadcrumbs came from: 'outline' (the PDF's own
    -- /Outlines), 'headings' (synthesized from the document's headings and
    -- clause numbering) or 'none'. Word writes contracts with no outline, so
    -- 'headings' is the normal case here -- and a reviewer looking at a
    -- citation that names a section deserves to know the section was inferred.
    spine_source  TEXT    NOT NULL DEFAULT 'none',
    ingested_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chunks (
    id              INTEGER PRIMARY KEY,
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    -- Position within the document; with document_id it identifies a chunk
    -- stably across re-ingestion.
    ordinal         INTEGER NOT NULL,
    content         TEXT    NOT NULL,
    -- 0-based physical page: what to open the file at.
    page            INTEGER,
    -- The *printed* page ("4", "A-2"), which is not the physical index
    -- whenever a contract numbers its exhibits separately from its body.
    -- This is the one a citation must show.
    page_label      TEXT,
    -- The last page the chunk's text touches, when its element was rejoined
    -- or stitched across a page break; NULL when it sits on one page.
    page_end        INTEGER,
    page_label_end  TEXT,
    section         TEXT,
    -- JSON breadcrumb: ["6. Identity, Access...", "6.6 Password Management"].
    section_path    TEXT,
    -- What kind of element this chunk came from: paragraph, table, figure...
    -- Retrieval and rendering both branch on it.
    element_type    TEXT    NOT NULL DEFAULT 'paragraph',
    -- JSON [x0, y0, x1, y1] on the page, for highlighting the cited region.
    bbox            TEXT,
    -- Figure image on disk. Assets live in data/assets/ rather than as blobs
    -- here, which keeps contracts.db small and the files inspectable.
    asset_path      TEXT,
    -- Structured payload the chunk text flattens: a table's markdown grid.
    payload         TEXT,
    token_count     INTEGER,
    -- Vectors from two different models look plausible together and are
    -- nonsense. Recording the model per row lets retrieval refuse to mix them.
    embedding_model TEXT    NOT NULL,
    UNIQUE (document_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks (document_id);

-- Brute-force KNN, no ANN index. Sub-millisecond at demo scale; the migration
-- path at real scale is pgvector or Qdrant behind the same interface.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0 (
    chunk_id    INTEGER PRIMARY KEY,
    -- A PARTITION KEY, not a plain metadata column: analysis is always scoped
    -- to one contract, sqlite-vec keeps each partition's vectors apart, and
    -- the constraint is applied *before* k. A scoped search therefore returns
    -- that document's true k nearest, not whatever survives a global top-k --
    -- which is what makes over-fetching and filtering in Python unnecessary.
    -- Left NULL, a row is invisible to every scoped query, so `_write` always
    -- supplies it and the ingest suite asserts no row lacks one.
    document_id INTEGER PARTITION KEY,
    embedding   FLOAT[{dim}]
);

-- External-content FTS5: the index stores no copy of the text, it points back
-- at chunks.id. That makes the triggers below mandatory, not an optimisation.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5 (
    content,
    content='chunks',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts (rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    -- 'delete' is FTS5's way of retracting a row from an external-content index.
    INSERT INTO chunks_fts (chunks_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
    DELETE FROM chunks_vec WHERE chunk_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts (chunks_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
    INSERT INTO chunks_fts (rowid, content) VALUES (new.id, new.content);
END;

-- One row per analysis: the durable half of a run.
--
-- Here rather than in a metrics file because this is not telemetry. The line
-- is that this file holds *what happened* -- the domain objects the system
-- stores -- while the metrics store holds *how it went* (spans, per-criterion
-- timings). An analysis is a domain object exactly as a document is, it is the
-- deliverable a client comes back for, and the API's storage must not depend
-- on the metrics module for it.
--
-- Written by `analyses.py`, from both surfaces: `analyze_document` records the
-- lifecycle, so `make analyze` populates the same table the API does and a
-- report produced on the command line is readable through GET /analyses/<id>.
-- (Angle brackets, not braces: db.py runs this file through str.format, so a
-- literal brace anywhere -- a comment included -- is a format placeholder.)
--
-- **No foreign key on document_id, and filename is denormalised beside it.**
-- Deleting a contract must not take the analyses with it: the report is the
-- deliverable, it is self-contained, and a record that vanishes when someone
-- tidies up the corpus is not a record. tests/test_analyses.py asserts it.
CREATE TABLE IF NOT EXISTS analyses (
    analysis_id        TEXT PRIMARY KEY,
    -- The id every log line of this run carries: the join key into app.jsonl,
    -- and into the metrics store's spans when it lands.
    trace_id           TEXT,
    document_id        INTEGER NOT NULL,
    filename           TEXT    NOT NULL DEFAULT '',
    -- Which surface asked for it: 'cli' for `make analyze`, and for an HTTP
    -- submission whatever `X-Surface` said -- 'api' when it said nothing,
    -- 'ui' from the browser, 'mcp' from the MCP connector. The KPI page
    -- slices on it, which it cannot do if every HTTP caller is 'api'.
    surface            TEXT    NOT NULL DEFAULT 'api',
    -- queued | running | done | failed | cancelled | interrupted.
    -- 'interrupted' is what `reconcile` writes over a row the process died
    -- holding: the model refusing and the machine going away are different
    -- events, and a client is told to run it again rather than that it failed.
    status             TEXT    NOT NULL DEFAULT 'queued',
    criteria_requested INTEGER NOT NULL DEFAULT 0,
    criteria_completed INTEGER NOT NULL DEFAULT 0,
    criteria_skipped   INTEGER NOT NULL DEFAULT 0,
    error              TEXT,
    -- The AnalysisReport verbatim -- the same bytes scripts/analyze.py --out
    -- writes, so there is still no second schema. ~30 KB for five criteria.
    report_json        TEXT,
    created_at         TEXT    NOT NULL,
    started_at         TEXT,
    completed_at       TEXT,

    -- Derived from the report on completion. Not KPI work smuggled in early:
    -- `finish_analysis` already holds the report to count the two criteria
    -- columns, and these are field reads off `report.totals` plus one
    -- comprehension over `report.results`. The metrics step inherits a
    -- populated table instead of a backfill.
    latency_s          REAL,
    cost_usd           REAL,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    tool_calls         INTEGER,
    needs_review       INTEGER,
    capped             INTEGER,
    mean_confidence    REAL,
    quotes_total       INTEGER,
    quotes_verified    INTEGER,

    -- How the Router closed each criterion out, and what the critic cost.
    -- The first three were declared before the Evaluator existed, on the
    -- argument that declaring a column costs nothing and removes an ALTER
    -- TABLE from the middle of a later step. That held for three of the five;
    -- the last two arrive by guarded ALTER in `db.apply_schema`, because
    -- `CREATE TABLE IF NOT EXISTS` cannot add a column to a database that is
    -- already there -- which is what every existing demo database is.
    evaluator_accepted    INTEGER,
    evaluator_revised     INTEGER,
    evaluator_fallback    INTEGER,
    evaluator_unevaluated INTEGER,
    evaluator_cost_usd    REAL
);

CREATE INDEX IF NOT EXISTS idx_analyses_document ON analyses (document_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_analyses_status ON analyses (status);
