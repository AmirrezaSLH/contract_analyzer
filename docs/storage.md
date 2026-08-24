# Storage

One SQLite file (`data/contracts.db`) holds the documents, the chunk text, the
vectors and the BM25 index. Hybrid retrieval is therefore a JOIN, not two
datastores to keep in sync, and the whole corpus can be copied, inspected with
the `sqlite3` CLI, or deleted with `rm`.

## Schema (`schema.sql`)

`schema.sql` is a template: `db.apply_schema(conn, dim)` substitutes `{dim}`
with the active embedder's width, because a `vec0` table fixes its vector
width at creation.

| Table | Role |
|---|---|
| `documents` | one row per ingested file: `path` (unique), `filename`, `content_hash` (SHA-256 -- re-ingesting an unchanged file is a no-op), `page_count`, `producer`, `has_outline`, `spine_source` (`outline` / `headings` / `none` -- where the section breadcrumbs came from, since Word writes no outline and a reviewer should know an inferred section when they see one), `ingested_at` |
| `chunks` | the source of truth. `content`, `page` (0-based physical), `page_label` (printed), `page_end` / `page_label_end` (set when the chunk crosses a page break), `section`, `section_path` (JSON breadcrumb), `element_type` (`paragraph`/`table`/`figure`…), `bbox`, `asset_path`, `payload` (a table's markdown grid), `token_count`, `embedding_model`; `UNIQUE(document_id, ordinal)` |
| `chunks_vec` | `vec0` virtual table, `embedding FLOAT[{dim}]`, queried by KNN. `document_id` is a `PARTITION KEY`: sqlite-vec keeps each contract's vectors apart and applies the constraint *before* `k`, so a document-scoped search returns that contract's true `k` nearest rather than the remains of a global top-k. A row written without it lands in the NULL partition and is invisible to every scoped query, so the ingest suite counts `chunks` against `chunks_vec` **per document** |
| `chunks_fts` | external-content FTS5 over `chunks.content` (`porter unicode61`), queried by BM25 |

Three triggers (`chunks_ai/ad/au`) keep `chunks_fts` and `chunks_vec` in step
with inserts, deletes and updates on `chunks`. With external-content FTS5 the
index stores no copy of the text, so the triggers are mandatory, not an
optimisation. `documents → chunks` cascades on delete, so replacing a document
is one `DELETE`.

`embedding_model` is recorded on every chunk row: vectors from two models look
plausible together and are nonsense, so retrieval refuses to query a corpus
built with a different model than the one in the process (the guard lives in
`embeddings/guard.py`).

## Connections (`db.py`)

* `connect(path, read_only=False, same_thread=True)` loads `sqlite-vec`, then
  **turns extension loading back off** -- leaving it on would turn any SQL
  injection into arbitrary code execution. WAL journal, foreign keys on,
  `sqlite3.Row` rows.
* `apply_schema(conn, dim)` checks the stored `chunks_vec` *before* running
  `CREATE VIRTUAL TABLE IF NOT EXISTS`, because that statement silently keeps
  an old definition. Two things are checked, both raising `SchemaMismatch`: a
  width that disagrees with `EMBEDDING_DIM`, and a table predating the
  document partition. A vec0 table can no more gain a partition key in place
  than it can change width, and without the second check the failure surfaces
  much later, from the first scoped search, as `no such column: document_id`.
* `get_db(settings)` is the one-call form: connect + schema for the configured
  path and dimension.
* `same_thread=False` exists for the later API, where one connection per
  request is handed to a worker thread. It is not a licence for concurrent
  use of one connection.

## The document catalogue (`documents.py`)

`db.py` opens the database, `ingest/` fills it, `retrieval/` ranks what is in
it. `documents.py` is the fourth thing a surface needs and none of those
provide: the queries that let a client bind a session to one contract before
any question is asked. Nothing here ranks, so it is not in `retrieval/`;
nothing here opens a connection, so it is not in `db.py`.

* `list_documents(conn, limit=None)` -- every document, newest first, each with
  its chunk count. `ingested_at` has one-second resolution, so `id` breaks the
  tie and two uploads in the same second still come back in order.
* `get_document(conn, id)` -- one `Document`, or `None`. Not an exception:
  every caller turns "no such id" into its own answer (a 404, a skip, a
  prompt).
* `document_sections(conn, id)` -- the outline as a list of `Section`s, in
  document order, built **from the chunks rather than from the parser**. A
  section that produced no chunk is not in the index, and offering it in a
  picker would be a promise retrieval cannot keep. Consecutive chunks sharing a
  breadcrumb collapse into one entry whose `page_display` spans all of them,
  formatted exactly like a citation's (`9`, or `9-10`).
* `delete_document(conn, id, remove_file=True)` -- one statement. `chunks`
  cascades from `documents` and the `chunks_ad` trigger takes the FTS and vec
  rows with it; the raw file goes too, unless it has wandered outside the
  project root, in which case it is left alone. Returns `False` for an unknown
  id so a caller can answer 404 without a second query.

SQLite only *promises* that a trigger fires for a direct delete -- for rows
removed by a foreign-key action the manual makes it depend on
`recursive_triggers`, which is off by default. It does fire on the versions
this project runs against; the test asserts the outcome rather than trusting
the mechanism.

## The `Chunk` record (`models.py`)

`Chunk` is a frozen dataclass mirroring the `chunks` columns minus
`document_id` and `embedding_model` -- what the chunker produces and the
pipeline stores. `.breadcrumb` renders `section_path` as `A > B > C`.

## Token counting (`tokens.py`)

`count_tokens(text)` uses `tiktoken`'s `cl100k_base` (the encoding behind
`text-embedding-3-small`) so the chunk budget is measured in the units the
embedder bills. A markdown table is ~2 tokens per cell and `len//4` is off by
~40 % on it. When `tiktoken` is missing *or* cannot fetch its BPE table
(offline, cold cache) it falls back to a rounded-up chars/4 estimate, once,
and `using_tiktoken()` reports which one is active.

## Scale note

KNN in `vec0` is brute force -- sub-millisecond for a few hundred chunks, fine
to a few hundred thousand. The retrieval interface hides the store; pgvector or
Qdrant is the migration behind the same `retrieve()` signature.
