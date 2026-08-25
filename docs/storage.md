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
| `analyses` | one row per analysis: identity and lifecycle (`analysis_id` PK, `trace_id`, `document_id`, `filename`, `surface` = `api`/`cli`, `status`, the three criteria counts, `error`, `report_json`, the three timestamps), the columns derived from the report on completion (`job_duration_s`, `cost_usd`, the token counts, `tool_calls`, `needs_review`, `capped`, `mean_confidence`, `quotes_total`, `quotes_verified`) and the five `evaluator_*` columns filled since the evaluator landed (`evaluator_accepted` / `_revised` / `_fallback` / `_unevaluated` -- how the Router closed each criterion out -- and `evaluator_cost_usd`) |

`analyses` lives here, beside `documents`, rather than in a metrics file. The
line is that **this schema holds *what happened* and the metrics store holds
*how it went***: an analysis is a domain object exactly as a document is, it is
the deliverable a client comes back for, and putting it here means the API's
storage never depends on the metrics module. `spans` and
`criterion_results` are the other side of that line: they are in
`metrics/metrics.sql`, applied by the metrics store on **this
same database file** -- two DDL files, one store, see [metrics.md](metrics.md).
It has to be a second file for a mechanical reason too, since `db.py` runs this
one through `str.format(dim=...)` and span DDL contains braces.

Three triggers (`chunks_ai/ad/au`) keep `chunks_fts` and `chunks_vec` in step
with inserts, deletes and updates on `chunks`. With external-content FTS5 the
index stores no copy of the text, so the triggers are mandatory, not an
optimisation. `documents → chunks` cascades on delete, so replacing a document
is one `DELETE`.

### The analysis record (`analyses.py`)

`documents.py` is the catalogue of contracts; `analyses.py` is the catalogue of
the work done on them. Same shape: plain functions over a connection, no
framework, importable by the CLI.

* `queue_analysis` / `mark_running` / `finish_analysis` / `fail_analysis` --
  the lifecycle. **`report.py` writes it, not the API.** `analyze_document`
  marks the run running on entry and finishes or fails it on exit, so `make
  analyze` populates the same table `POST /analyses` does and a report produced
  on the command line is readable through `GET /analyses/{id}`. The job runner
  writes exactly one state the CLI has no equivalent for: `queued`.
* `mark_running` is an **upsert**, which is what makes both paths work: it
  transitions the API's queued row, and it creates the CLI's from nothing.
* `reconcile(conn)` -- called once from the API's lifespan, before anything is
  served. Rows a killed process left at `queued` or `running` become
  **`interrupted`**, not `failed`: nothing refused, the machine went away, and
  the two want different KPI treatment and different UI copy.
* `get_analysis` / `list_analyses` / `live_analyses` -- reads. `None` for an
  unknown id, like `get_document`.

Two deliberate denormalisations. **`document_id` carries no foreign key** and
`filename` is copied onto the row, so `DELETE /documents/{id}` cannot take the
analyses with it -- the report is the deliverable, it is self-contained, and a
record that vanishes because somebody tidied up the corpus is not a record.
And `report_json` is the `AnalysisReport` verbatim, the same bytes
`scripts/analyze.py --out` writes, so there is no second schema to drift.
Measured at **~30 KB for a five-criterion report**: a thousand analyses is
about 30 MB. There is no retention policy and none is planned until asked for.

The derived columns are filled at the same time rather than left to the metrics
store, because `finish_analysis` is already holding the report in order to
count the criteria: the totals are field reads and the quote counts are one
comprehension. The metrics step inherits a populated table instead of a
backfill.

**A known inconsistency, recorded rather than fixed.** `documents.ingested_at`
is SQLite's `datetime('now')` (`2026-08-24 04:30:50`) while every timestamp
`analyses` mints is ISO-8601 with an offset (`2026-08-24T04:30:50+00:00`). A
query comparing the two would be comparing formats. Nothing joins on it today,
so it is left alone and fixed when something needs it.

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
