# Step 9 — hybrid retrieval scoped to one contract

**Status: implemented, 2026-08-23** (`d40db7d`, `0bab21f`, `8509b02`). Refines
step 9 of `01_02_chunking_retrieval_plan.md` in light of what checkpoint 3
landed and of two findings below. Steps 10–14 are unchanged. What was built
differs from this plan in four places, all of them from the review below; see
[What landed](#what-landed).

## Where we stand

Checkpoint 3 (commits `3c7045f`…`a972f8b`) put 102 chunks of the sample
contract into the four tables, every one with a breadcrumb, `page_end` where
the text crosses a break, and `documents.spine_source` recording that the
sections were inferred. `chunks` = `chunks_vec` = `chunks_fts` = 102.
Retrieval is the first thing that reads any of it.

The shape is the source project's: two retrievers over one database,
`chunks_vec` for meaning and `chunks_fts` for exact words, fused by Reciprocal
Rank Fusion, behind a single `retrieve()`. What changes is that analysis here
is scoped to **one uploaded contract**, and that the parser now supplies
structure worth querying directly.

## Two findings that change the plan

### 1. sqlite-vec can filter KNN exactly. The over-fetch design is obsolete.

`01_02_chunking_retrieval_plan.md` open question 2 offered a choice between
over-fetching `k * 4` and filtering in Python, or one `chunks_vec` table per
document. Neither is necessary: the installed **sqlite-vec 0.1.9** supports
metadata columns and `PARTITION KEY` on a `vec0` table, and applies the filter
**before** `k` rather than after.

Verified on this machine:

```sql
CREATE VIRTUAL TABLE p USING vec0(
    chunk_id    INTEGER PRIMARY KEY,
    document_id INTEGER PARTITION KEY,
    embedding   FLOAT[4]
);
SELECT chunk_id, distance FROM p
 WHERE embedding MATCH ? AND k = 2 AND document_id = 2 ORDER BY distance;
```

returns document 2's two nearest vectors -- *including* one at distance 1.41 --
rather than the global top-2 filtered down to whatever survives. A query with
no `document_id` still searches the whole corpus, and `DELETE FROM p WHERE
chunk_id = ?` still works, so the `chunks_ad` trigger is unaffected.

So: **`chunks_vec` gains `document_id INTEGER PARTITION KEY`.** A partition key
rather than a plain metadata column because the access pattern is exactly
per-document -- sqlite-vec stores each partition separately, so a scoped query
scans only that contract's vectors. This is exact, cheaper than over-fetching,
and removes the truncation risk the plan wanted recorded in the timings. The
`RetrievalResult` over-fetch factor is dropped with it.

Cost: a schema line and one more column in the `chunks_vec` insert. The
database has never been built, so there is no migration. `db.stored_dim`'s
regex reads `embedding FLOAT[{dim}]` and is unaffected.

### 2. `text_for_model` would throw away the breadcrumb on every table

`RetrievedChunk.text_for_model` returns `payload` for a table -- the **bare**
markdown grid. That is correct for rendering and wrong for the model: commit
`3c7045f` put the breadcrumb in front of a table's `content` precisely because
a Word contract's requirement matrix has no caption and its cells say
"Password rotation" while never saying "Password Management Standard". Sending
`payload` alone hands the answer model a grid with no idea which section it
came from, undoing the fix one step downstream of it.

Fix in `base.py`: `text_for_model` returns `breadcrumb + "\n" + payload` for a
table with a payload, `content` otherwise. Asserted by a test that fails on the
bare grid.

### Corrections to the earlier plan

* I flagged FTS5's bare-hyphen crash as work for this step. It is already
  handled: `escape_query` quotes every term, so `GOV-01` becomes the phrase
  `"gov 01"` and matches the cell. Ported as is, with its test.
* The plan's acceptance criterion says `GOV-01` should return "the Exhibit A
  row chunk". Measured: `GOV-01` appears in exactly **one** chunk, the
  stitched `Exhibit G — Security Schedule > G1. Governance and Risk
  Management` table (ordinal 79). The criterion is corrected below.

## Changes, module by module

Copied from the source project, then changed in these places and no others.

### `schema.sql` + `ingest/pipeline.py`

* `chunks_vec` gains `document_id INTEGER PARTITION KEY`.
* `_write` inserts `(chunk_id, document_id, embedding)`.
* No change to the triggers, the cascade, or `db.py`.

### `retrieval/base.py`

* `RetrievedChunk` gains `page_end`, `page_label_end` and a `page_display`
  property using the same rule as `Chunk.page_display`, so a citation reads
  `p.9-10` when the clause spans a break.
* `citation_title` becomes `"{filename} — {breadcrumb} (p.{page_display})"`,
  falling back gracefully when there is no breadcrumb. The breadcrumb is what
  makes a citation checkable against a contract; a filename and a page number
  alone make the reviewer hunt.
* `text_for_model` fixed as above.
* `_SELECT` gains `c.page_end`, `c.page_label_end` and `d.spine_source`, and
  `RetrievedChunk` carries `spine_source` so a surface can mark an inferred
  section without a second query. (Cheap: `documents` is already joined.)
* `hydrate` unchanged -- it already re-orders off a dict so the ranking
  survives `WHERE id IN (...)`, and a chunk deleted between search and fetch
  drops out rather than raising.
* `similarity_from_distance`, `RetrievalResult` unchanged except for a
  `document_id` field.

### `retrieval/vector.py`

* `vector_search(conn, vector, *, k, document_id=None)`; when given, the KNN
  query adds `AND document_id = ?`. `k` stays mandatory (a `vec0` KNN query
  without it is an error, not a full scan).
* `embed_question` unchanged -- `embed_query`, never `embed_documents`, since
  the asymmetric prefix is the whole reason those are two methods.

### `retrieval/keyword.py`

* `keyword_search(conn, question, *, k, document_id=None)`; when given, the
  BM25 SQL joins `chunks` and adds `AND c.document_id = ?`.
* `ORDER BY score` stays **ascending** -- `bm25()` is negated and lower is
  better, and `DESC` would return real matches that are the worst ones, so
  nothing would look broken.
* `escape_query` unchanged.

### `retrieval/sections.py` (new)

```python
retrieve_by_section(conn, document_id, pattern, *, limit=20) -> list[RetrievedChunk]
```

Pure SQL, no embedding, for Phase B's router hints ("6.6", "Exhibit G"). The
pattern is matched against the JSON breadcrumb as `section_path LIKE '%"' ||
pattern`, which anchors it to the **start of a path component**: `6.6%` finds
`"6.6 Password Management Standard"` at any depth and does *not* match
`"16.6 ..."`. Results are ordered by `ordinal`, so a multi-chunk section reads
in document order.

A separate module rather than a fourth branch of `retrieve()`: this is
structural lookup, it returns no ranking, and it needs neither an embedder nor
a mode.

### `retrieval/hybrid.py`

* `retrieve(question, conn, embedder, settings, *, mode, top_k, candidates,
  document_id=None)`. `None` keeps corpus-wide behaviour for the CLI; every
  Phase B caller passes an id.
* `document_id` threaded to both retrievers; `RetrievalResult` records it.
* `rrf_fuse` unchanged (`rrf_k=60`), including the `chunk_id`-ascending
  tie-break, without which two chunks a single retriever returned at adjacent
  ranks tie exactly and the eval harness reports a different hit@5 on
  identical data.
* `NEEDS_EMBEDDER` unchanged: `keyword` never embeds, which is what makes it
  the mode that works offline with no key.

## Commit sequence

Feature before test, so `make test` is green at every commit.

| # | Commit | What |
|---|---|---|
| 9a ✅ `d40db7d` | `feat(storage): partition chunks_vec by document` | `schema.sql`, `pipeline._write`. Exact per-document KNN. |
| 9b ✅ `0bab21f` | `feat(retrieval): hybrid vector+BM25 retrieval scoped to one document` | `retrieval/{__init__,base,vector,keyword,sections,hybrid}.py` |
| 9c ✅ `8509b02` | `test: retrieval suite -- scoping, fusion, section lookup` | `tests/test_retrieval.py`, `conftest` gains `ingested_sample` |
| 9d ✅ | `docs(retrieval): two retrievers, one fusion, one contract` | `docs/retrieval.md`; `architecture.md` status line |

Estimated effort: ~1.5 h. Actual: ~1.5 h, plus the four changes below.

## Tests (9c)

`conftest.py` gains a session fixture `ingested_sample`: the sample contract
plus a second, synthetic contract ingested into one temporary database with
`FakeEmbedder`. Two documents is the point -- scoping cannot be tested with one.

**Unit, no database:**
* `rrf_fuse` — agreement beats enthusiasm: a chunk both retrievers put 3rd
  outranks one a single retriever put 1st; ties break on `chunk_id`; a
  retriever that did not return a chunk contributes no term.
* `escape_query` — every term quoted; joined with `OR` not `AND`; pure
  punctuation yields `""`; `GOV-01` and `TLS 1.2` become phrases.
* `similarity_from_distance` — `d=0 → 1.0`, clamped.
* `citation_title` and `page_display` — `p.9-10` on a spanning chunk.
* `text_for_model` — a table's text carries its breadcrumb (finding 2).

**Against the two-document database:**
* `keyword` for `GOV-01` returns the `Exhibit G > G1. Governance and Risk
  Management` table chunk **first**. This is the parser's `GOV- 01` fix, the
  chunker's breadcrumb fix and BM25 escaping asserted end to end.
* `keyword` for `password rotation break-glass credentials` puts §6.6 or the
  `G3A. Password Management` table in the top 3.
* `retrieve_by_section(doc, "6.6%")` returns exactly the 6.6 chunk; `"Exhibit
  G%"` returns the 15 chunks under it; `"16.6%"` returns none.
* **Scoping, in all three modes:** every result's `document_id` is the one
  asked for, and a term unique to the other contract returns nothing.
* **Vector scoping is exact, not filtered:** with the other document holding
  the nearer vectors, a scoped search still returns `k` results from the
  target document rather than the remainder of an over-fetch.
* `mode="keyword"` works with `embedder=None` — the offline path.
* `mode="vector"` raises `ValueError` without an embedder, and
  `check_query_model` is called *before* the question is embedded.
* An empty database returns an empty `RetrievalResult`, not an exception.
* `top_k` deeper than `candidates` widens the pool instead of truncating.

## Acceptance

- [x] `make test` green (209 tests, up from 161), `make lint` clean, no module
      imports `logging`.
- [ ] `make search Q="GOV-01" --mode keyword` → the Exhibit G G1 row first.
      **Deferred to step 11**, which is where `scripts/search.py` is written;
      `scripts/` is still empty. The equivalent is asserted in the suite
      (`test_keyword_finds_the_hyphenated_identifier`) and was run by hand:
      keyword `GOV-01` returns ordinal 79, `Exhibit G — Security Schedule >
      G1. Governance and Risk Management`, first and alone.
- [x] `retrieve(..., document_id=N)` returns nothing from any other document
      in any mode, with the vector side filtered by the partition key rather
      than after the fact.
- [x] A citation line shows section and printed page, `p.9-10` where the chunk
      spans a break (the *leaf* section -- see resolved question 3).
- [x] Re-ingesting still leaves `chunks` = `chunks_vec` = `chunks_fts`, now
      asserted per document rather than in total.

## Open questions, as resolved

1. **`spine_source` on `RetrievedChunk`** — carried. `documents` is joined for
   the filename anyway, so a surface can mark an inferred section without a
   second query. Asserted: the sample's chunks come back `spine_source =
   'headings'`.
2. **`retrieve_by_section` pattern semantics** — anchored, *and the anchoring
   moved inside the function*. See change 3 below.
3. **`citation_title` format** — the **leaf** section, not the full path:
   `Sample Contract.pdf — 6.6 Password Management Standard (p.9-10)`. A deep
   breadcrumb is longer than the line it has to fit on, and the leaf plus the
   page is what a reviewer needs to find the clause; the full path is still on
   the object as `.breadcrumb` for a surface with room for it. Each part is
   dropped rather than faked when missing, so a chunk with no section still
   gets a usable title.

## What landed

The two findings above were implemented as written. Four things changed during
the review of this plan, all of them defended in the code they touch:

1. **The stale-database guard** (`db.apply_schema`). `CREATE VIRTUAL TABLE IF
   NOT EXISTS` keeps an existing definition, and the guard only compared
   *width* — so a `chunks_vec` built before 9a would survive, and the failure
   would surface much later as `no such column: document_id` from the first
   scoped search. `apply_schema` now also checks for the partition key and
   raises `SchemaMismatch` with what to do about it. There was such a database
   on the machine at the time (`ingest_smoke/c.db`), so "the database has never
   been built" was not quite true.
2. **The NULL partition is asserted against.** A vec0 row written without its
   partition value is accepted, lands in the NULL partition, and is invisible
   to every scoped query — while `chunks` = `chunks_vec` still tallies. The
   acceptance criterion is therefore counted **per document**, which is the
   only form of it that fails on that mistake.
3. **`retrieve_by_section` takes a prefix, not a LIKE expression.** As planned,
   the caller supplied the trailing `%` (`"6.6%"`), which means
   `retrieve_by_section(conn, doc, "6.6")` would return nothing rather than
   erroring — a JSON path never ends mid-component. The function now builds
   `'%"' || ? || '%' ESCAPE '\'` itself and escapes `%`, `_` and `\`, so a
   router hint containing one is read literally (`6_6` no longer finds `6.6`).
4. **`document_id` is required, and corpus-wide is spelled `ALL_DOCUMENTS`.**
   Defaulting to `None` means a Phase B call site that forgets the argument
   does not raise: it answers a question about one contract with another
   contract's clause, in a well-formed citation. Making the scope explicit
   costs the CLI one keyword.

Measured on the two-document test corpus, scoped to the sample contract:

| Query | Mode | First result |
|---|---|---|
| `GOV-01` | keyword | ordinal 79, `Exhibit G … > G1. Governance and Risk Management` (table), and nothing else matches |
| `password rotation break-glass credentials` | keyword | `G3A. Password Management (Added)`, then `6.6 Password Management Standard` |
| `retrieve_by_section("6.6")` | — | the one 6.6 chunk; `"Exhibit G"` → 15 chunks in document order; `"16.6"` → none |

Timings on this corpus are sub-millisecond per retriever (`retrieve()` records
`embed_ms` / `vector_ms` / `keyword_ms` / `hydrate_ms` in `RetrievalResult`,
which is what the KPI page reads later).

Deferred, deliberately: `scripts/search.py` and `make search` (step 11), and
the eval harness that would make hit@5 a number rather than a claim.
