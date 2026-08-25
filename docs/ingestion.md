# Ingestion and embeddings

What `src/contract_analyzer/ingest/pipeline.py` and
`src/contract_analyzer/embeddings/` do, and the properties they are built to
guarantee. Numbers were measured on `data/samples/Sample Contract.pdf` and are
asserted by `tests/test_ingest.py` and `tests/test_embeddings.py`.

## Purpose

One function turns a file on disk into rows in the four tables:

```bash
make ingest F="data/samples/Sample Contract.pdf"
make reingest F="data/samples/Sample Contract.pdf"   # rebuild regardless
```

```python
from contract_analyzer.ingest import ingest_file

result = ingest_file(path, conn, embedder, settings)
result.status        # ingested | replaced | skipped | failed | dry-run
result.chunks, result.pages, result.spine_source, result.elapsed
result.report        # the ChunkingReport: what was dropped and why
```

`parse -> chunk -> embed -> store`, once per file. Three properties are worth
more than the code that provides them.

## Re-ingesting an unchanged file is free

The file's SHA-256 is compared against `documents.content_hash` **before
parsing**, not after. Parsing costs about a second per contract and embedding
costs money, and neither should be spent to discover that nothing changed.

This is a claim about *cost*, so the test asserts cost: `FakeEmbedder` counts
the texts it is handed, and the second run must leave that counter untouched.
A row-count assertion would pass even if the whole contract were re-embedded
and written identically -- which is precisely the bug the check exists to
prevent.

Measured: first run 102 chunks in 1.05 s with 102 embedder calls; second run
`skipped`, **zero** calls.

## A changed file is replaced whole

The `documents` row is deleted and `ON DELETE CASCADE` takes `chunks` with it;
the `chunks_ad` trigger retracts each row from `chunks_fts` and deletes from
`chunks_vec`. Then the new version is written. Incremental per-page
re-ingestion is explicitly out of scope: at a hundred chunks, rebuilding takes
a second and has no failure modes.

Replacing also clears the document's extracted figures. The parser
de-duplicates assets by content hash *within* a run, not across runs, so
without the clear a figure that moved or vanished in the new version would
leave an orphan file that a stale `asset_path` could still cite.

The whole write is one transaction. A `documents` row without its chunks would
be reported as ingested and answer nothing; chunks without vectors would be
invisible to KNN and visible to BM25. `with conn` commits on success and rolls
back on anything else.

## One file's failure is one file's problem

A PDF that raises is recorded as `failed` with the exception in `result.error`
and the run continues. A batch must not be hostage to its worst file. The one
exception that escapes is `ModelMismatch`, because that is a fact about the
whole run rather than about one file.

## The model guard

Vectors from two different embedders are points in unrelated spaces. Mixed,
they do not error, do not look wrong, and do not return nothing -- they return
a ranking that is plausible and meaningless, and nothing about the symptom
points at the cause. So `check_embedding_model` runs once, up front, before
any parsing: discovering the mismatch on file two means a wasted parse and an
embedding bill for file one.

`guard.py` lives in `embeddings/` rather than in `ingest/` because retrieval
needs the same rule on the read path, and the read path must not import the
write path. The two entry points say different things because they have
different fixes:

* `check_embedding_model` -- refuse to **add** to another model's corpus.
* `check_query_model` -- refuse to **answer** against one. The sentence has to
  explain that every ranking returned would be noise, since a results list
  that looks fine is not self-evidently wrong.

## The embedders

Three interchangeable backends behind one protocol. Provider modules are
imported only when selected, so importing the package needs neither an API key
nor the ~800 MB `[local]` extra.

| Provider | Model | Dim | Needs |
|---|---|---|---|
| `openai` (default) | `text-embedding-3-small` | 512 (truncated) | `OPENAI_API_KEY` |
| `local` | `BAAI/bge-small-en-v1.5` | 384 (fixed) | `pip install -e ".[local]"` |
| `fake` | `fake-hash-<dim>` | any | nothing |

**Width is checked twice, on purpose.** `config.py` checks *intent* -- that the
configured width is one the provider can emit. `BaseEmbedder` measures the
first vector that actually comes back, because a `vec0` table rejects a
wrong-width vector with an error naming neither the model nor the cause, and
only after a contract has been parsed and paid for.

**`openai` truncates and re-normalises.** The `dimensions` parameter asks for a
Matryoshka prefix of the full 1536-dim vector; 512 costs a little accuracy and
cuts a brute-force KNN scan to a third of the work. A prefix of a unit vector
is *not* unit length, and `vec0` ranks by L2, which agrees with cosine only on
unit vectors -- so without re-normalisation the ranking silently drifts toward
whichever chunks have the largest prefix norm. Nothing errors; the results just
get worse.

**`local` is asymmetric.** bge was trained with an instruction prefix on the
query side only. Prepending it to passages as well does not "keep things
consistent", it moves every document toward the same region and flattens the
ranking -- which is the entire reason `embed_documents` and `embed_query` are
two methods rather than one `embed()`.

**`fake` has no semantics and says so.** It hashes words onto the axes with
`blake2b` -- not the builtin `hash()`, which is randomised per process, so the
same text must give the same vector tomorrow or idempotency is not testable.
Two paraphrases sharing no vocabulary are orthogonal under it. Its name lands
in `chunks.embedding_model` on every row, so a database built with it announces
itself and the guard refuses to add real vectors to it. It exists so the suite
never touches the network and so the pipeline demos with no key; offline,
keyword mode is the one carrying real signal, which on a contract full of
`PASS-02` and `TLS 1.2` is further than it sounds.

## One retry policy

The OpenAI SDK is constructed with `max_retries=0` on the shared client from
`http_client.py`, so the transport's policy is the only loop that runs. Two
live loops multiply: four intended attempts become sixteen requests, and the
only symptom in production is a bill and a rate limit. `tests/test_embeddings.py`
counts requests through a `MockTransport` to prove there is exactly one.

When the policy is exhausted the SDK wraps the failure in an
`APIConnectionError`; it is unwrapped so the caller gets the one-line
`HttpFailure` naming the URL, the attempts and the elapsed time. A 401 becomes
`EmbedderUnavailable` instead, pointing at `EMBEDDING_PROVIDER=fake` -- it is a
configuration error and should read like one.

## What gets written

`documents` records `path` (relative to the project root, so the database is
portable), `filename`, `content_hash`, `page_count`, `producer`, `has_outline`
and **`spine_source`**.

`spine_source` is there because Word writes contracts with no `/Outlines`: on
this corpus every breadcrumb was inferred from the document's own headings and
clause numbering. A reviewer reading a citation that names "6.6 Password
Management Standard" deserves to know whether the contract said that or the
parser worked it out. It is carried out on `IngestResult` too, including for a
skipped file, so the CLI can print it without re-opening the PDF.

`chunks` gets the `Chunk` record one-for-one, with `section_path` and `bbox`
as JSON and `page_end` NULL rather than a repeat of `page` for a single-page
chunk. `chunks_vec` and `chunks_fts` follow from the insert -- the FTS index is
filled by the `chunks_ai` trigger, so the pipeline never writes to it directly.
See [storage.md](storage.md).

## Observability

Every stage runs inside a `span()` from `logger.py`, under one trace id per
file:

```
ingest.file
  ingest.parse    pages, elements, sections, spine_source
  ingest.chunk    chunks, plus every ChunkingReport counter
  ingest.embed    chunks, model, tokens, cost_usd
  ingest.write    chunks
```

So `.run/app.jsonl` carries the same timings the ingest report prints, and an
`http.retry` from the embedder appears under the file that provoked it. This is
the seam the KPI store hangs off: its handler files each of these `span.end`
records as a row without this module knowing it exists. See
[metrics.md](metrics.md).

`ingest.embed` is the one with a dollar on it. `usage.total_tokens` off the
embeddings response is priced through `generation/pricing.py`, and the local
and fake embedders report zero tokens because they bill nothing. It is
**captured but never tiled**: at about $0.0002 for the sample against a ~$0.96
analysis, it is a sentence in the waterfall, not a number on a dashboard.

## Measured on the sample contract

| Measurement | Value |
|---|---|
| Pages / elements / chunks | 21 / 164 / 102 |
| `spine_source` / `has_outline` | `headings` / 0 |
| First run | 1.05 s, 102 embedder calls |
| Second run | `skipped`, 0 embedder calls |
| `--reingest` | `replaced`, 1 document row, 102 chunks |
| `chunks` = `chunks_vec` = `chunks_fts` | 102 |
| Chunks with a stored page range | 11 |
| `producer` | `Microsoft® Word for Microsoft 365` |

## Known limitations

- **No incremental update.** A one-character edit re-embeds the whole
  contract. At this scale that is a second and a fiftieth of a cent; at corpus
  scale it would want chunk-level hashing.
- **`dry_run` parses and chunks but cannot report embedding cost by model**,
  only token counts against `OPENAI_COST_PER_TOKEN`.
- **Figure description (`--describe-figures`) is opt-in and off.** It is never
  fatal -- a missing description is a nice-to-have -- and the sample contract
  has no figures.
- **`KNOWN_SUFFIXES` is `.pdf` only.** A `.docx` loader emitting the same
  elements would add its suffix here and change nothing else.
- **The vector width is fixed at database creation.** Changing `EMBEDDING_DIM`
  requires a rebuild; `db.py` refuses to open a mismatched database rather
  than letting `vec0` fail later.
