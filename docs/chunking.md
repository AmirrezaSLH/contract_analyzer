# Chunking

What `src/contract_analyzer/ingest/chunker.py` does, why each rule is the way
it is, and the contract it hands to the embedder. Every number quoted was
measured on `data/samples/Sample Contract.pdf` and is asserted by
`tests/test_chunker.py`.

## Purpose

The parser produces elements; retrieval consumes *chunks*. They are not the
same thing. An element is one thing on a page. A chunk is one unit of
retrieval: the text that gets embedded, the text BM25 indexes, and the text a
citation points at. Packing one into the other is where retrieval quality is
decided, because no amount of clever ranking recovers a chunk that split an
obligation in half or that claims a section it is only partly in.

```python
from contract_analyzer.ingest import ChunkingReport, chunk_document

report = ChunkingReport()
chunks = chunk_document(parsed, settings, report=report)
chunks[0].content        # "6. Identity, ... > 6.6 Password Management Standard\n6.6 ..."
chunks[0].section_path   # the breadcrumb as a list
chunks[0].page_display   # "9" or "9-10"
report.as_dict()         # what was dropped, and why
```

The job is smaller here than it would have been before the parser hardening.
With clause-level elements, a breadcrumb on all 164 of them, and tables
already stitched across page breaks, **most chunks are exactly one clause**
and most tables fit whole. What is left is a set of boundary decisions.

## The rule carried over from the parser

> **Nothing is dropped by name.**

This is the parser's rule -- a decision may only use evidence the document
supplies about itself -- applied to selection. The corpus this chunker
descends from dropped "References" and "Bibliography" by title, which was
safe for a thesis and would be fatal here: a contract's exhibits, schedules
and annexes *are* the evidence a compliance question is answered from
(Exhibit G holds the numbered `PASS-` requirements), and "Definitions" is what
a defined term resolves to.

Two things are refused an index entry, both because they are pointers to
content rather than content:

* **a contents row** -- a dot leader followed by the page it points at
  (`_DOT_LEADER_ROW`). A query for "password management" that retrieves the
  table-of-contents line instead of clause 6.6 has failed.
* **a front-matter section** -- but *only when `spine_source == "outline"`*.
  This is the half worth explaining. When the PDF carries its own
  `/Outlines`, the outline says where a section ends, so "Table of Contents"
  can be trusted to name front matter. When the spine was **synthesized** from
  headings and clause numbering, a "Contents" heading owns every element until
  the next heading the synthesizer managed to detect -- which on a contract is
  real clauses. Inferred structure is not evidence that a section is
  disposable, so `spine_source` is threaded into `chunk_elements()` and the
  rule switches off. On the sample, which has no outline, `dropped_front_matter`
  is 0 by construction.

`ChunkingReport` counts every drop by reason. Silent deletion is the hardest
bug to see from outside: a contract that lost its exhibits and a contract that
never had any look identical in the database.

## What a chunk is

| Element | Becomes |
|---|---|
| paragraph / clause / caption | packed into a run of same-section prose |
| table | a chunk of its own, never split across a boundary |
| figure | a chunk of its own, with its caption and the prose that cites it |
| heading | **not a chunk** -- it is already in every breadcrumb below it |
| equation | **never a chunk alone** -- folded into the prose around it |
| furniture | never reaches the chunker |

A heading is excluded because it is near-empty text that would compete in BM25
with the sections it names -- and because it is not lost: it is the first line
of every chunk beneath it.

## The decisions

### One section per chunk

A chunk closes when the section path changes. A chunk spanning 6.6 and 6.7
would cite one and contain both, which makes its citation a lie about half its
text -- worse than failing to answer. On this contract the rule is strict
enough that a *section boundary*, not the token budget, is what closes almost
every chunk: 100 sections over 164 elements.

### Every chunk opens with its breadcrumb -- tables included

A chunk is read alone: by BM25, by the embedder, by the answer model. Six
tokens of `6. Identity, Access, Authentication, and Password Management > 6.6
Password Management Standard` is what tells the reader which obligation they
are looking at.

For tables this is not a nicety, it is the fix that makes an exhibit findable.
Word contracts carry no table captions, so a requirements matrix arrives as a
bare grid whose cells say "Password rotation" and never say "Password
Management Standard". Prefixing the section is what lets a keyword search for
the section name reach the row. The `payload` column keeps the **bare**
markdown so a UI renders a grid rather than a heading with a grid under it.

The breadcrumb is paid for **out of** the budget, not added on top of it
(`_budget`). Breadcrumbs here reach ~25 tokens.

### Overlap: whole elements, then a sentence tail

Overlap exists so a chunk does not open in the middle of a thought. The rule
that used to enforce it -- carry only whole trailing elements -- was written
for a corpus whose elements were single wrapped lines of ~20 tokens, where
three or four always fitted an 80-token budget. Here an element is a clause,
and the longest runs to ~290 tokens, so a strict whole-element rule carries
**nothing** exactly when the context is most worth having.

So `_overlap_seed` carries whole elements where they fit and, where they do
not, the trailing *sentences* of the last one. That preserves the property the
whole-element rule was standing in for -- a chunk never begins mid-sentence --
and if even the final sentence is over budget, nothing is carried, because a
cut sentence is worse than no overlap. The seed never includes the group's
first piece, so packing always advances.

**On this contract, overlap never fires.** `overlap_chunks` is 0: no section
is large enough for the 400-token budget to be what closes a chunk. The rules
are correct and are proved on synthetic elements
(`test_overlap_falls_back_to_the_trailing_sentences_of_a_big_clause`); this
document simply does not exercise them. The zero is asserted rather than
glossed over, so a contract with longer sections shows up as a failing test
rather than as a silent change in behaviour.

`CHUNK_OVERLAP_TOKENS=80` against `CHUNK_TOKENS=400` is 20% of the budget.

### The minimum length floor is low on purpose

`MIN_CHUNK_TOKENS = 8`, not the 20 a prose corpus wants. A real clause can be
one sentence -- *"Either party may terminate this Agreement on thirty days
written notice."* is 14 tokens -- and a floor tuned for paragraphs would
delete an obligation. The floor is applied to the body, not to body plus
breadcrumb, so a long section title cannot keep a three-word chunk alive.

One chunk is dropped on the sample: the paragraph `"5.3 Control Objectives."`,
whose entire text is its own section title and which therefore survives in the
breadcrumb of the table beneath it. Section 5.3 still has a chunk.

### Tables split by rows, with the header repeated

A table over budget is split by rows, each part carrying the header and a
`(part i/n)` marker so it is readable alone. The alternative is a grid
silently truncated wherever the budget happened to fall. One table on the
sample is over budget and splits.

### Page span comes from the chunk's own elements

`page` is the anchor's start page; `page_end` / `page_label_end` are the union
over the chunk's **non-overlap** pieces. Borrowed context comes from earlier
pages and must not widen the range a citation claims. A clause the parser
rejoined across a page break, or a table it stitched, genuinely continues, and
`page_display` renders `9-10`. Eleven of the sample's 102 chunks span a break.

### Figures carry the prose that cites them

A caption alone is true and nearly unretrievable. The paragraph that refers to
the figure by number holds the words a reader would actually search for, so a
sentence or two is quoted into the chunk. The sample contract has no figures;
the machinery is kept because it costs nothing and a different contract may.

## The contract to the next stage

1. `chunk_elements(elements, settings)` is a **pure function**: same input,
   same chunks, byte for byte. Idempotent ingestion depends on it.
2. `ordinal` is dense and starts at 0.
3. Every chunk's `content` starts with its breadcrumb when it has one.
4. No chunk exceeds `CHUNK_TOKENS`, except a row-split remainder.
5. No chunk crosses a section boundary.
6. `payload` is structure the text flattens -- a table's bare markdown grid.
7. `page_end` is `None`, never a repeat of `page`, when the chunk is on one
   page, so `p.4` and `p.4-5` stay distinguishable in SQL.
8. The input is `list[Element]`, not `ParsedDocument`: a `.docx` or `.md`
   loader emitting elements changes nothing downstream.

## Measured on the sample contract

`chunk_document(parse_pdf("data/samples/Sample Contract.pdf"), settings)` with
`CHUNK_TOKENS=400`, `CHUNK_OVERLAP_TOKENS=80`:

| Measurement | Value |
|---|---|
| Elements in | 164 |
| Chunks out | 102 (67 paragraph, 35 table) |
| Headings folded into breadcrumbs | 51 |
| Tokens per chunk: min / p50 / p95 / max | 28 / 70 / 213 / 383 |
| Chunks over budget | 0 |
| Chunks with a non-empty `section_path` | 102 of 102 |
| Chunks whose text opens with the breadcrumb | 102 of 102 |
| Chunks spanning a page break | 11 |
| `dropped_contents` / `dropped_front_matter` | 0 / 0 |
| `oversized_elements_split` / `tables_split` | 0 / 1 |
| `dropped_short_chunks` | 1 (a section title, see above) |
| `overlap_chunks` / `overlap_tokens` | 0 / 0 |
| Requirement identifiers reaching the index | 12 distinct, `GOV-01` in the stitched G1 matrix |

The predicted range in `plan_implement_docs/01_02_chunking_retrieval_plan.md`
was 150-200 chunks. That prediction was made before the parser began emitting
clauses as their own elements; with a section boundary closing nearly every
chunk, 102 is the honest number.

## Known limitations

- **Overlap is untested by this corpus.** See above. The first contract with
  sections longer than 400 tokens is the one that exercises it.
- **A chunk cites its elements' page span, not its quote's position.**
  Inherited from the parser: mapping a quoted sentence back to the exact page
  of a rejoined clause needs per-line offsets the merge does not keep.
- **The section rule is stricter than it needs to be.** It closes on any
  change of section path, not only a level-1 change, so two short sibling
  subsections never share a chunk even when both would fit. That costs some
  packing efficiency and buys citation precision; on a contract, precision is
  the one worth having.
- **`_LABEL` only resolves "Figure N" and "Table N" mentions**, not "Exhibit
  G" or "Schedule 2". Exhibits are sections here, so they are already in the
  breadcrumb; extending the mention map would duplicate that.
- **No semantic or late chunking.** Packing is structural, because the
  structure is real and recovered. A document with no numbering and no
  headings would fall back to one long run per empty section path, which is
  the case the `spine_source == "none"` path exists to make visible.
