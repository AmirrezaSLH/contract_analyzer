# Phase A — implementation report (in progress)

Companion to `01_foundation_plan.md`. Updated at each checkpoint; the final
version is written when the phase closes.

## Checkpoint 1 — 2026-08-23

### Commits

| Commit | Plan step | Notes |
|---|---|---|
| `da87f42 docs: implementation plan…` | – | plan files + `AGENTS.md` |
| `c93800c chore: project scaffold` | 1 | `pyproject.toml`, `Makefile`, `.env.example`, `.gitignore`; criteria JSON moved into the package |
| `f19a71c feat(core): logger…` | 2 | `logger.py` + 6 tests |
| `b3bb72a feat(core): retrying HTTP client…` | 3 | `http_client.py` + 13 tests |
| `8d62097 style: wrap two long lines` | – | ruff E501 |
| `e3b6eb4 feat(core): settings, SQLite store…` | 4 | `config.py`, `db.py`, `schema.sql`, `tokens.py` |
| `5a0b763 feat(parse): PyMuPDF parser…` | 5 | `parse/` + `models.py`; `describe.py` on logger + shared HTTP client |

### Verification

* `pytest`: 20 passed, offline. `ruff`: clean.
* Copied parser on `Sample Contract.pdf`: 21 pages, 51 headings, 52
  paragraphs, 42 ruled tables, 0 furniture, **0 sections** (no outline) --
  the gap step 6 closes.

### Deviations from the plan

* **`httpx` → `httpx2`.** anthropic 1.0 and openai 3.3 require an
  `httpx2.Client`. Same API; `http_client.py` aliases it and `pyproject`
  depends on `httpx2`.
* `models.py` moved from step 4 to step 5 because it imports `parse.elements`.
* Environment: `pip` and `git commit` each segfaulted once (likely the
  `/media` mount); no data lost, retried successfully. The project `.venv`
  install is slow on this mount; tests were run with RAG_Mock's venv
  (`PYTHONPATH=src`).

### Remaining (at checkpoint 1)

Steps 6–14: section spine synthesis, chunker/pipeline, embeddings, retrieval
with `document_id`, cited chat, CLIs, test port, sample parse report, docs.

## Checkpoint 2 — 2026-08-23 — parser hardening (replaces step 6)

Executed `02_parser_hardening_plan.md` in full. Every commit was verified
against the sample contract before it landed.

### Commits

| Commit | Plan step | Notes |
|---|---|---|
| `4f2d89d docs: parser audit and hardening plan` | 6a | the audit |
| `b2d5f54 chore: ignore AGENTS.md` | – | safeguard for the repo rule |
| `4777487 test: parser regression suite…` | 6b | `conftest.py`, `test_parse_elements.py`, `test_parse_tables.py`; 61 tests, written before the fixes and red until 6h |
| `ac58c49 fix(parse): resolve line-break hyphens…` | 6c | P3 |
| `5cb1346 fix(parse): keep numbered clauses…` | 6d | P2; new `parse/enumerators.py` |
| `a445338 feat(parse): synthesize section spine…` | 6e | P1 |
| `02e3f09 fix(parse): stitch tables across page breaks` | 6f | P4; `page_end` / `page_label_end` introduced here rather than in 6g, because the stitched table is the first element that needs them |
| `6e38d30 fix(parse): page spans on merged elements` | 6g | P5; `Chunk` and `schema.sql` gain the pair |
| `fdc905b fix(parse): tighten furniture detection` | 6h | P6; bands measured, `caption_band` uses the measured edge |
| `eae0f42 fix(parse): judge a continuation by the last line's right edge` | – | found by the 6b suite: after a cross-page merge the first line's edge was deciding whether the paragraph had ended |
| `8514662 docs: update parsing.md…` | 6i | plus `architecture.md` status line |

### Verification

* `pytest`: 81 passed (20 foundation + 61 parser), offline; sample-contract
  tests skip when the PDF is absent. `ruff`: clean.
* Sample contract, after: 51 headings / 79 paragraphs / 34 tables /
  0 furniture; `spine_source="headings"`, 100 sections, 164/164 elements
  with a breadcrumb; 49/49 clauses standalone (largest paragraph 1,164
  chars, was 2,727); 0/48 control IDs corrupted; 8 tables and 2 paragraphs
  carry `page_end`; `breaks_hyphenate=False`, bands 0.046/0.952; 0.92 s.
* Text conservation asserted as an identity: page characters ==
  element characters + the dropped duplicate header rows.

### Deviations from the hardening plan

* **Hard-wrap rule.** The plan's "concatenation attested, fragment not" test
  fails in practice because a wrapped fragment always attests itself (the
  wrapped cell is in the vocabulary) and one-letter stubs (`g`, `t`, `e`)
  are frequent tokens on their own (`e.g.`). The rule as implemented:
  the concatenation must be attested strictly more often than the *longer*
  fragment. Same principle, corrected measurement.
* **Cells repaired: 68, not 71.** The plan's number came from a simulation
  before the rule above was corrected; the measured count with the shipped
  rule is 68 of 266 wrapped cells. `Progres`/`s` stays split: `progress`
  occurs nowhere else in the document, so there is no evidence to join it.
* **Lettered and roman enumerators** are recognised and stop a merge but do
  not enter the spine or split an element; only integer, decimal, alnum and
  exhibit labels are *sectional*.
* The `.venv` in the working tree was corrupt (pip itself would not import)
  and was rebuilt; `make test` runs from it now.

### Remaining

Phase A steps 7–14, unchanged: chunker/pipeline (keep exhibits; breadcrumb on
table chunks; `page_end` through to the row), embeddings, retrieval with
`document_id`, cited chat, CLIs, test port, sample parse report, docs.
