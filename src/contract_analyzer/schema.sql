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

CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY,
    path          TEXT    NOT NULL UNIQUE,
    filename      TEXT    NOT NULL,
    -- Hash of the file's bytes: re-ingesting an unchanged file is a no-op.
    content_hash  TEXT    NOT NULL,
    page_count    INTEGER,
    -- Diagnostics: which tool produced the PDF, and whether it carried an
    -- outline. A file with no outline had its sections inferred from font
    -- size, which is worth knowing when a citation looks wrong.
    producer      TEXT,
    has_outline   INTEGER NOT NULL DEFAULT 0,
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
    -- The *printed* page ("37", "xxi"), which is not the physical index
    -- whenever there is roman-numbered front matter -- an offset of 13 and 20
    -- pages in the two corpus files. This is the one a citation must show.
    page_label      TEXT,
    -- The last page the chunk's text touches, when its element was rejoined
    -- or stitched across a page break; NULL when it sits on one page.
    page_end        INTEGER,
    page_label_end  TEXT,
    section         TEXT,
    -- JSON breadcrumb: ["2 Background...", "2.1 Building Airtightness"].
    section_path    TEXT,
    -- What kind of element this chunk came from: paragraph, table, figure...
    -- Retrieval and rendering both branch on it.
    element_type    TEXT    NOT NULL DEFAULT 'paragraph',
    -- JSON [x0, y0, x1, y1] on the page, for highlighting the cited region.
    bbox            TEXT,
    -- Figure image on disk. Assets live in data/assets/ rather than as blobs
    -- here, which keeps rag.db small and the files inspectable.
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
    chunk_id  INTEGER PRIMARY KEY,
    embedding FLOAT[{dim}]
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
