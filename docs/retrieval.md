# Retrieval

What `src/contract_analyzer/retrieval/` does. Two retrievers over the one
database, fused by Reciprocal Rank Fusion, scoped to a single contract.
Numbers were measured on `data/samples/Sample Contract.pdf` ingested alongside
a synthetic decoy, and are asserted by `tests/test_retrieval.py`.

## Purpose

```python
from contract_analyzer.retrieval import ALL_DOCUMENTS, retrieve, retrieve_by_section

result = retrieve(question, conn, embedder, settings, document_id=doc_id)
result.mode, result.document_id, result.timings   # hybrid | vector | keyword
for chunk in result:
    chunk.citation_title      # Sample Contract.pdf — 6.6 Password Management (p.9-10)
    chunk.text_for_model()    # what the answer model is shown
    chunk.score, chunk.ranks  # 0.031, {"vector": 7, "keyword": 1}

retrieve_by_section(conn, doc_id, "Exhibit G")    # structure, no embedder, no ranking
```

`retrieve()` ranks chunk ids, fuses the two rankings, and reads the rows for
the survivors — exactly `top_k` of them. Everything a citation needs comes back
on the chunk.

## Two retrievers, because contracts are two kinds of text at once

| | `chunks_vec` (KNN) | `chunks_fts` (BM25) |
|---|---|---|
| Finds | paraphrase: "secure admin pathway" ≈ "bastion host" | the exact string: `GOV-01`, `TLS 1.2`, `PASS-02` |
| Misses | identifiers, which it blurs into "a security control" | any wording the contract did not use |
| Needs | an embedder, a key, a network round trip | nothing |

Compliance language mixes the two in one sentence, which is why neither
retriever alone is the answer and why `keyword` is a real offline mode rather
than a degraded one.

## Fusion: ranks, not scores

```
score(chunk) = Σ over retrievers  1 / (rrf_k + rank)      rrf_k = 60
```

An L2 distance and a BM25 score are not on the same scale, and no weighting of
them is defensible without a labelled set to tune on. RRF avoids the question:
it says only that *a chunk both retrievers thought was reasonable beats one
that a single retriever loved*. With `rrf_k = 60` the gap between rank 1 and
rank 2 is small, so agreement outweighs position.

Two details:

* **`candidates` is deeper than `top_k`** (20 vs 6 by default). Fusion can only
  rank a chunk that at least one retriever returned, so the pool must be wider
  than the answer. Asking for a `top_k` deeper than `candidates` widens the
  pool rather than truncating the answer.
* **Ties break on ascending `chunk_id`.** Two chunks a single retriever
  returned at adjacent ranks tie exactly whenever the other retriever returned
  neither; without a deterministic tie-break, an eval harness reports a
  different hit@5 on identical data.

Single-retriever modes do not fuse — they keep the retriever's own number, a
cosine similarity or a negated BM25 score, because those are worth reading.
Every `score` in this package means *higher is better*.

## Scoping is exact, not filtered

`chunks_vec` is partitioned by `document_id` (see
[storage.md](storage.md#schema-schemasql)). sqlite-vec applies a `PARTITION
KEY` constraint **before** `k`, so a scoped search returns that contract's true
`k` nearest chunks and scans only that contract's vectors.

The rejected alternative was to over-fetch `k * 4` globally and filter in
Python, which returns fewer than `k` results — or none — exactly when the other
contracts in the database hold the more similar text. The test makes that
concrete: a decoy contract repeating the sample's password vocabulary owns
every slot of an unscoped top-5, while the scoped search still returns five
chunks of the sample.

The BM25 side needs no such trick: it joins `chunks` and filters before the
`LIMIT`.

**`document_id` is a required argument**, and searching everything is spelled
`ALL_DOCUMENTS`. A default of "the whole corpus" would mean a call site that
forgot the scope does not fail — it answers a question about one contract with
another contract's clause, in a well-formed citation.

## Citations

`RetrievedChunk` carries the joined `documents` row, so a surface renders a
citation without a second query.

* `citation_title` → `Sample Contract.pdf — 6.6 Password Management Standard
  (p.9-10)`. The **leaf** section: a deep path is longer than the line it has
  to fit on, and the leaf plus the page is what lets a reviewer find the
  clause. `.breadcrumb` still holds the full path.
* `page_display` prints the *printed* page, and a range when the chunk's
  element was stitched across a page break — `p.9-10`, never a silent `p.9`.
* `spine_source` says whether the section was read from the PDF's outline or
  inferred. The sample is a Word contract with no outline, so its citations are
  `headings`, and a reviewer deserves to know that.
* `text_for_model()` returns a **table's grid with its breadcrumb in front**.
  The grid is the readable form; the breadcrumb is the only thing that says
  which section's requirements these rows are — a requirement matrix's cells
  say "Password rotation" and never "6.6 Password Management Standard".

## Keyword escaping

FTS5 reads bare punctuation as syntax: `GOV-01` is a column filter followed by
a negation, and raises `fts5: syntax error near "-"`. `escape_query` quotes
every term, so `GOV-01` becomes the phrase `"gov 01"` — which is what the
tokenizer stored anyway — and `TLS 1.2` becomes `"tls" OR "1 2"`.

Terms are joined with **OR**, not AND: a retriever ranks, it does not filter,
and a five-word question whose fifth word appears nowhere should return the
chunks matching the other four. BM25 already rewards the chunks that match
more of them. A question with no word characters returns no results rather
than an FTS5 error.

## Structural lookup

```python
retrieve_by_section(conn, document_id, "6.6")          # the 6.6 chunk
retrieve_by_section(conn, document_id, "Exhibit G")    # all 15, in document order
```

Phase B's router knows which section a criterion lives in long before it knows
which sentence answers it. This is that lookup: pure SQL over the JSON
breadcrumb, no embedder, no key, no ranking, ordered by `ordinal` so a section
split across chunks reads in order.

The pattern is a **plain prefix**, anchored to the start of a breadcrumb
component — `6.6` matches `6.6 Password Management Standard` at any depth and
does not match `16.6 Force Majeure`. LIKE wildcards in the pattern are escaped,
so a router hint containing `_` is read literally rather than matching any
character.

A separate module rather than a fourth mode of `retrieve()`: it takes no
question, returns no scores, and its order is the document's.

## Modes and guards

| Mode | Embedder | What runs |
|---|---|---|
| `hybrid` (default) | required | KNN + BM25, fused by RRF |
| `vector` | required | KNN only, scored by cosine similarity |
| `keyword` | **not needed** | BM25 only, scored by negated `bm25()` |

* A mode that embeds raises `ValueError` without an embedder.
* `check_query_model` runs **before** the question is embedded: it is one
  SELECT over a tiny distinct set, and what it saves is an HTTP round trip and
  a charge. A corpus built by another model raises `ModelMismatch` rather than
  returning a plausible, meaningless ranking.
* An empty database returns an empty `RetrievalResult`, not an exception.
* A chunk deleted between the search and the hydration drops out of the
  results rather than raising.

## Measured

Scoped to the sample contract, on the two-document test corpus:

| Query | Mode | Result |
|---|---|---|
| `GOV-01` | keyword | ordinal 79, `Exhibit G — Security Schedule > G1. Governance and Risk Management` (table), the only match in the contract |
| `password rotation break-glass credentials` | keyword | `G3A. Password Management (Added)`, then `6.6 Password Management Standard` |
| `GOV-01` | hybrid | the same table first, `{"vector": 7, "keyword": 1}` — agreement lifting what one side found |
| `retrieve_by_section("Exhibit G")` | — | 15 chunks, document order |

`RetrievalResult.timings` records `embed_ms`, `vector_ms`, `keyword_ms` and
`hydrate_ms`; all four are sub-millisecond at this scale, and the KPI page
reads them later. Vector search is brute force in `vec0` — fine to a few
hundred thousand chunks, and the interface hides the store when it is not.
