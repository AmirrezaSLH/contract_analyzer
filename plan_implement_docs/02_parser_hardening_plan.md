# Parser hardening — reading the sample contract without defects

Audit of `src/contract_analyzer/parse/` against `data/samples/Sample Contract.pdf`
(21-page "Information Security and Technology Risk Addendum", Microsoft Word for
Microsoft 365 export), and the plan to close what it found.

Every number below was measured on that file at commit `519cade`, not estimated.
The audit method is at the end so the numbers can be reproduced and re-checked
after the fixes land.

**Scope.** `parse/` only: the element stream handed to the chunker. Chunking,
embedding, retrieval and generation are Phase A commits 7-10 and unchanged here.

**The overfitting constraint.** The point is not to make one PDF parse. Every
fix below is required to derive its decision from evidence *the document itself
supplies* — its own vocabulary, its own numbering, its own geometry — rather
than from a constant chosen because it happened to suit this addendum. Section
"The generalizing principle" states the rule each fix is held to, and
"Rejected because they overfit" records the shortcuts deliberately not taken.

---

## Summary

The parser is in better shape than the commit-5 status line suggests. Text
handling is exactly conservative, table *detection* is perfect on this file, and
heading detection recovers every real heading with no false positives. Four
defects stand between it and a flawless read, one of them already planned as
commit 6.

| # | Defect | Impact on this file | Severity |
|---|---|---|---|
| P1 | No section spine (no `/Outlines`) | `section` empty on all 145 elements | **blocker** |
| P2 | Consecutive clauses welded into one element | 26 clauses buried inside 8 hosts; largest element ≈681 tokens | **blocker** |
| P3 | Line-break hyphens resolved with the wrong default | 48 control IDs corrupted (`GOV-01` → `GOV- 01`); `just-in-time` → `just-intime` in prose | **high** |
| P4 | Page-spanning tables split in two | 8 tables → 16 elements | medium |
| P5 | Cross-page merged paragraph cites its first page only | 3 elements; quotes can cite the wrong page | medium |
| P6 | `_BARE_NUMBER` matches ordinary words | latent — 0 occurrences here | low |
| P7 | Dead code from the LaTeX origin | none | cosmetic |

P1 and P2 interact and must be sequenced: see "Why P2 comes before P1".

---

## What is already correct — do not change it

Recording this deliberately. Half the value of an audit is preventing churn in
the parts that work, and several of these look wrong at a glance.

**Text is exactly conserved.** Stripping to alphanumerics, the characters on the
page and the characters in `elements + furniture` match at a ratio of
**1.0000** (30,768 = 30,768). Nothing is lost and nothing is indexed twice. The
claim-rectangle ordering in `parse_pdf` (tables claim, then figures, then text
with `exclude=claimed`) and the caption de-duplication in `_caption_rects` both
do exactly what `docs/parsing.md` says. Guarantees 1, 2, 4, 5, 6 and 8 were
re-verified programmatically and all hold.

**Table detection is perfect on this file.** All 42 tables land on rung 1
(`strategy="lines"`, quality `ruled`) and all 42 pass `validate()`. Word draws
every cell border as a real line, so the booktabs recovery rungs never fire.
The *contents* of the cells need work (P3); the detection does not.

**Heading detection is perfect on this file.** 51 headings: 21 top-level
(`1.` … `21.`), 8 Exhibit headings, 21 Exhibit G/H sub-headings (`G1.` … `G13.`,
`H1.` … `H6.`, including `G3A. Password Management (Added)`), the document
title, and `Signatures`. No real heading missed, no false positive. The
character-weighted modal body size (12.0pt) and the 0.5pt margin do the work.

**`furniture: 0` is correct, not a failure.** The document has no running header
and no page number — verified by inspecting every block in the header/footer
bands and the last block of each page. There is nothing to drop. `docs/parsing.md`
hedges on this ("either the addendum carries no running header … or they sit in
a block that also holds body text"); it is the former, and the doc should be
corrected.

**Exhibit G's many small tables are correct, not a split.** Pages 13-18 produce
~20 tables sharing the header `ID | Requirement | Minimum Standard | …`. These
are *not* fragments of one table: each is the requirement table of its own `G`
subsection, separated by a real `G1.`/`G2.`/`G3.` heading. Only the 8 cases in
P4 are genuine splits. A stitcher that merges on matching headers alone would
destroy this structure — hence the "nothing between them" condition in P4.

**Performance is a non-issue.** 0.95s for 21 pages, single-threaded.

---

## The generalizing principle

The parser already states the right philosophy in `blocks.py`: measure the
document, then judge relative to that measurement. `body_size` is not
hard-coded to 12pt, it is the character-weighted mode. Hyphens are not resolved
from a dictionary, they are resolved from the document's own vocabulary.

The defects below are all places where that philosophy was *not* followed —
where a constant from the LaTeX corpus stands in for a measurement. So the rule
every fix is held to:

> **A decision may only use evidence the document supplies about itself.**
> If a threshold cannot be derived from the profile, the signal must be
> structural (numbering that forms a coherent sequence) or lexical (a token
> attested in this document's vocabulary) — never a literal tuned to one file.

Two corollaries used repeatedly below:

* **Corroboration over pattern-matching.** A regex says a string *looks like* a
  clause number. Whether it *is* one is decided by whether it takes its place in
  an ascending sequence with its siblings. This is what separates `6.3` (follows
  `6.2`) from `SAML 2.0` (belongs to no sequence).
* **Degrade, don't guess.** When the evidence is absent, record that it is
  absent (`spine_source = "none"`) and leave the field empty. A wrong section
  breadcrumb on a compliance citation is worse than a blank one.

---

## P2 — Consecutive clauses are welded into one element

**Fix first.** P1 is built on top of the element stream P2 corrupts.

### Evidence

`join_wrapped_lines` merges across 11 of the 41 clause boundaries on this file,
burying 26 clauses inside 8 host elements:

```
p3   1523ch  '3.3 "Security Incident" means…'   also contains 3.4 3.5 3.6 3.7 3.8 3.9
p4   2727ch  '6.2 MFA. Vendor will enforce…'    also contains 6.3 6.4 6.5 6.6 6.7
p8   1264ch  '13.1 Minimization…'               also contains 13.2 … 13.7
```

The 2,727-char element is roughly 681 tokens against a 400-token chunk target,
so the chunker will split it on a boundary unrelated to clause structure.

**This directly damages the deliverable.** Two of the five compliance criteria
are `6.6 Password Management Standard` and `7.2 Data in Transit Requirements
(TLS)`. Both are currently buried mid-element rather than being addressable,
retrievable, citable units.

### Root cause

`_continues()` in `pdf.py` requires the candidate line to start flush at
`body_left`. In LaTeX that is decisive, because `\parindent` indents the first
line of every paragraph, so a flush-left line is a continuation by construction.
Word does not indent — it separates paragraphs with space-after. Measured over
the pre-merge paragraph stream:

| | new clause begins (must **not** merge) | wrapped line (**should** merge) |
|---|---|---|
| left edge == `body_left` (36pt) | **41 of 41** | **28 of 28** |
| vertical gap, median | 14.7pt | 25.7pt |
| gap range | 14.6 – 386.8 | 14.6 – 294.3 |

`starts_at_text_left` has **zero** discriminating power here: it is true for
every line of both kinds. What is currently preventing the other 30 bad merges
is the *other* test, `reaches_text_width(prev)` — a clause's last line usually
stops short of the right margin. The 11 failures are the clauses whose
predecessor happened to end near-full-width.

**Retuning `_MAX_LINE_GAP` cannot fix this.** The gap distributions are
inverted — new clauses sit *tighter* (14.7pt) than genuine wrapped lines
(25.7pt) — and overlap from 14.6pt upward. Any threshold strict enough to block
the bad merges blocks more good ones. The geometry in this document does not
separate the two cases, so no geometric constant can.

### Fix — an enumerator lattice, corroborated by sequence

Add a document-wide pass that identifies *enumerators* (the label that opens a
numbered clause) and validates them as sequences, then let `_continues()` veto
a merge when the candidate opens a corroborated enumerator.

**Step 1 — candidate enumerators.** A small family of shapes, covering the ways
legal and technical documents number things, not just this one:

| shape | example | seen here |
|---|---|---|
| decimal | `6.6`, `12.4.1` | yes |
| alphanumeric prefix | `G3A.`, `PASS-02` | yes (Exhibit G) |
| lettered | `(a)`, `(iv)` | yes (inline) |
| roman | `IV.`, `Article IV` | no |
| bare integer | `21.` | yes (top level) |

**Step 2 — corroborate by sequence.** A candidate is a real enumerator only if
its siblings appear in ascending document order: `6.1 → 6.2 → 6.3` is a
sequence; a lone `2.0` is not. Concretely, group candidates by their parent key
(`6.` for `6.3`) and require the group to have ≥2 members appearing in
non-decreasing order at non-decreasing document positions.

This is the test that makes the rule safe. There are **29** decimal-number
occurrences mid-prose on this file, and the corroboration correctly separates
them:

```
'…SAML 2.0 SSO is supported…'      → rejected: no 2.x sequence; preceded by a word
'…expiration date. 3.8 "Vendor…'   → accepted: member of 3.1…3.9, follows a terminator
```

Two cheap local conditions supplement it, both document-independent: the
enumerator must be preceded by a sentence terminator or start the block, and
followed by capitalised text or an opening quote.

**Step 3 — veto in `_continues()`.** Return `False` when `element` opens a
corroborated enumerator. Add it as an early guard; leave the existing geometric
tests in place, since they carry the LaTeX corpus and remain correct there.

**Step 4 — retire the dead test.** `starts_at_text_left` on the *candidate*
line stays (harmless, and load-bearing for LaTeX). The `_INDENT`-tolerance check
on `prev` is what encodes `\parindent`; gate it on evidence that the document
indents at all — measurable from the profile as a bimodal left-edge
distribution. On this file the distribution is unimodal at 36pt, so the test
disables itself. That is the principle applied to its own constant.

**Step 5 — split the existing welds.** The veto prevents *new* welds. Elements
already welded (should any survive, e.g. from a single PyMuPDF block containing
two clauses) are split on corroborated enumerator positions, with each fragment
inheriting the geometry of the line it starts on.

### Acceptance

* 41 of 41 clause boundaries preserved; 0 clauses buried inside another element.
* `6.6 Password Management Standard` and `7.2 Data in Transit Requirements` are
  each their own element.
* No element over 4,000 characters; largest paragraph under the 400-token target
  or split only at a clause boundary.
* Text conservation ratio stays exactly 1.0000.
* The LaTeX regression corpus (if retained) parses unchanged.

---

## P1 — No section spine

Already planned as commit 6; this audit confirms the diagnosis and tightens the
design in light of P2.

### Evidence

`has_outline = False`, `sections = []`, and `section` / `section_path` are empty
on **all 145** content elements. Word's PDF export writes no `/Outlines`, so
`build_spine` returns `[]` and `assign_sections` has nothing to assign. Every
chunk would reach the index with a blank breadcrumb.

### Why P2 comes before P1

Commit 6 plans to synthesize the spine partly from `^\d{1,2}\.\d{1,2}\s+Title.`
prefixes on paragraphs. 26 of those prefixes are currently *not* at the start of
an element — they are buried mid-text by P2. Building the spine first would
silently recover a fraction of the structure and look like it worked.

### Fix — `synthesize_spine(elements, profile)`

Reuses the P2 enumerator lattice rather than a second set of regexes; the two
must not disagree about what a clause label is.

1. **Level 1** from `heading` elements — already perfect here (21 numbered
   sections + 8 Exhibits + the title).
2. **Level 2** from corroborated enumerators opening a `paragraph`, taking the
   title as the text up to the first sentence terminator (`6.6 Password
   Management Standard.` → `Password Management Standard`).
3. **Nesting from the enumerator itself**, not from font size: `6.6` is a child
   of `6.`, `G3A.` a child of `Exhibit G`. Depth is the enumerator's own depth,
   which is what makes this generalize to `12.4.1`.
4. **`start_y`** is the element's own `bbox[1]`, so `assign_sections` — which
   already handles pinned positions correctly — needs no change.
5. **Record provenance**: `ParsedDocument.spine_source = "outline" | "headings"
   | "none"`, so a report can say how the structure was obtained and a
   downstream consumer can distrust a synthesized spine if it wants to.

### Acceptance

* `spine_source == "headings"`; every one of the 145 elements has a non-empty
  `section_path`, except any front-matter element genuinely preceding the first
  heading.
* Breadcrumb for the password clause reads
  `6. Identity, Access, Authentication, and Password Management > 6.6 Password Management Standard`.
* On a PDF that *does* carry `/Outlines`, `spine_source == "outline"` and the
  synthesized path is not used — the existing behaviour is untouched.

---

## P3 — Line-break hyphens are resolved with the wrong default

Two related faults: table cells never reach the hyphen resolver at all, and the
resolver's fallback is tuned for a document class this one does not belong to.
The second also corrupts prose, so this is not a tables-only defect.

### Evidence — cells

**48 of 981 cells** carry a corrupted identifier:

```
'GOV- 01'  'GOV- 02'  'ASSET- 01'  'IAM- 01' … 'IAM- 06'
'Monitoring/Alertin g'   'Requiremen t Ref'   'Included In- Scope?'
```

**Zero** prose elements have the same corruption. That asymmetry is the whole
diagnosis: prose goes through `join_lines`, table cells do not.

This matters more than a cosmetic blemish. `docs/architecture.md` justifies
hybrid retrieval on exactly this case — "compliance language mixes exact jargon
(`TLS 1.2`, `PASS-02`, `SAML`) that BM25 nails". A control matrix whose IDs are
stored as `GOV- 01` cannot be found by BM25 on `GOV-01`, and a compliance
finding cannot cite the control by its ID. The audit found the defect precisely
in the field the design leans on.

### Root cause — cells

`page.find_tables().extract()` **does** preserve the intra-cell line break —
43 of 70 raw cells on page 14 contain `\n`:

```python
'GOV-\n01'
'Maintain a written information\nsecurity program aligned to a\nrecognized framework'
```

`compact()` then calls `normalize_ws()` on each cell, which collapses `\n` to a
space and destroys the information. The break is available; it is thrown away
one function too early.

### Evidence — prose, and the wrong default

`join_lines` resolves a line-final hyphen by asking the document's vocabulary,
and when neither form is attested it **drops** the hyphen. That fallback is
documented as "the common case by roughly four to one in this corpus" — the
LaTeX corpus. Measured over every line-final hyphen in this contract:

| | count |
|---|---|
| compound attested → keep the hyphen (`in-scope`, `semi-annual`) | 10 |
| merged form attested → drop it (typographic hyphen) | **0** |
| neither attested → today's default drops it | 3 |

**Word does not hyphenate at line breaks** (auto-hyphenation is off by default),
so every line-final hyphen in this document is lexical. The correct action is
"keep" in 13 of 13 cases, and the current default is wrong in all 3 ambiguous
ones — including one already visible in today's prose output:

```
p4 paragraph: '…(b) implementing just-intime access or time-bou…'   # just-in-time
cells:        'token-|based' → 'tokenbased'    'out-of-|region' → 'ofregion'
```

So the defect is not only that cells miss the resolver; it is that the
resolver's fallback encodes a property of LaTeX typesetting rather than a
property of the document in front of it.

### Fix — resolve every break from measured evidence

Make cell text follow the same path as prose text. `compact()` takes the
profile, splits each cell on `\n`, and runs the joiner over the fragments —
exactly what `block_text()` already does for a prose block.

`join_lines` needs two extensions first, both driven by document evidence, both
of which also improve prose:

1. **Letter-digit boundaries — keep the hyphen.** `join_lines` currently
   requires the continuation to begin with `[A-Za-z]`, so `GOV-` + `01` falls
   through to the space-join, giving `GOV- 01`.

   Widening the tail to `[A-Za-z0-9]` is *not* sufficient, and the naive version
   of this fix is actively wrong. Traced against the profile: with a wider tail,
   `head='GOV'`, `tail='01'`, `compound='gov-01'` — which
   `profile.hyphenated` does **not** contain, because the vocabulary collector
   regex is `[A-Za-z]+(?:-[A-Za-z]+)+`, letters only. The attestation test
   therefore fails and the code falls through to *drop* the hyphen, yielding
   `GOV01`. Worse, `GOV-01` never appears unwrapped anywhere in the document
   (0 occurrences), so no amount of vocabulary lookup can attest that exact
   token.

   Two changes, and the second is what makes it work:

   * Widen the vocabulary collector to capture letter-digit compounds, so
     identifiers that *do* appear unwrapped are attested. The document contains
     **21 distinct** such identifiers (`ASSET-01`, `PASS-02`, `IAM-01`,
     `CRYP-01`, `BCDR-02`, `DATA-01`, …).
   * Add a structural rule that needs no attestation: **a hyphen at a
     letter→digit boundary is never dropped.** A line-break hyphen is a
     typographic artifact inserted inside a word; English does not hyphenate a
     word across a letter-to-digit boundary, so such a hyphen is lexical —
     part of the token — in `GOV-01`, `ISO-27001`, `SOC-2`, `TLS-1.2`. This
     repairs `GOV-01` even though `GOV-01` itself is attested nowhere.

   The document still supplies the corroboration, just at the level of *shape*
   rather than literal: 21 attested `[A-Z]{2,}-\d{2}` tokens demonstrate that
   this shape is a real identifier pattern in this document, which is the
   evidence that the structural rule is the right one to apply here. Learning
   the shape rather than the literal is what lets it generalize to a contract
   whose controls are numbered `AC-2` or `A.9.4.3`.
2. **Hard wraps with no hyphen.** A narrow cell wraps mid-word with no hyphen at
   all: `Monitoring/Alertin` + `g`. Rule: if concatenating the fragments without
   a space yields a token attested in `profile.words`, concatenate; otherwise
   join with a space. Verified against the profile — `alerting` occurs 8 times,
   `requirement` 23, `monitoring` 10 — so the document supplies the evidence for
   its own repair, and a genuine two-word cell (`Special Handling`) is untouched
   because `specialhandling` is attested nowhere.

3. **Measure the fallback instead of assuming it.** Add
   `DocumentProfile.breaks_hyphenate: bool`, computed in `profile_document` from
   the counts in the evidence table above: over all line-final hyphens, compare
   how often the merged form is attested against how often the compound is. A
   document that auto-hyphenates (LaTeX) shows merged forms dominating and keeps
   today's drop-by-default; one that does not (Word, and this contract at 10:0)
   flips to keep-by-default.

   This is the principle applied to the last remaining constant. It fixes
   `just-in-time`, `token-based` and `out-of-region` without a list of
   exceptions, and it leaves the LaTeX corpus on exactly the behaviour it has
   today — the same code path, a different measurement.

### Validation

The three rules were simulated over all **266** wrapped cells in the sample
contract before being proposed. They change **71** cells, every change an
improvement, with no over-joins detected:

```
'GOV-|01'                      -> 'GOV-01'              (rule 1, all 48 IDs)
'Monitoring/Alertin|g'         -> 'Monitoring/Alerting' (rule 2)
'Included In-|Scope?'          -> 'Included In-Scope?'  (vocabulary, unchanged)
'Maintain inventory of in-|scope assets' -> '… in-scope assets'
'semi-|annual'                 -> 'semi-annual'
'N/A (token-|based)'           -> 'N/A (token-based)'   (rule 3; 'tokenbased' without it)
```

Genuine two-word cells (`Special Handling`, `Access Type`) are left alone. The
simulation is the basis of the commit 6b tests.

Note `rows_to_markdown` already escapes `\n` to `<br>`, which is currently
unreachable (0 cells contain `\n` after `compact()`). Once cells are joined
properly it stays unreachable for wrapped lines — correctly, since those are one
logical line — and becomes reachable only for genuinely multi-line cells.

### Acceptance

* 0 cells matching `[A-Z][A-Z0-9]*-\s+\w`; all 21 attested control IDs intact.
* `just-in-time`, `token-based`, `out-of-region` correct in prose and cells;
  `profile.breaks_hyphenate is False` on this document.
* `Monitoring/Alerting`, `Requirement Ref`, `Included In-Scope?` correct.
* A BM25 query for `GOV-01` returns the Exhibit G row (verifiable after commit 7).
* No regression in prose: the prose corruption count stays 0.

---

## P4 — Page-spanning tables are split in two

### Evidence

8 genuine splits — identical header row, consecutive pages, **no element
between them**:

```
p2→p3   6x3 + 4x3   Component/Record Type | Included In-Scope? | Notes
p6→p7   4x4 + 2x4   Asset Type | Coverage Requirement | Monitoring/Alerting | …
p10→p11 13x4 + 8x4  Control Domain | Control Requirement | Minimum Standard | …
p11→p12 3x4 + 6x4   Step | Data Movement | Example | Control Points
p12→p13 3x5 + 3x5   Role | Name/Title | Email | Phone | Escalation Hours
p14→p15 4x5 + 4x5   ID | Requirement | Minimum Standard | …
p16→p17 3x5 + 3x5   ID | Requirement | Minimum Standard | …
p19→p20 3x10 + 3x10 Risk ID | Finding / Gap | Requirement Ref | …
```

The 13x4 Exhibit A control matrix arriving as 13 rows then 8 rows means a chunk
holds half the control set, and a "which controls are required?" question
retrieves half an answer with no indication the rest exists.

### Fix — stitch on evidence, with a hard guard

In `parse_pdf`, after per-page extraction and before `join_wrapped_lines`, merge
table B into table A when **all** hold:

1. `B.page_index == A.page_index + 1` (strictly the next page);
2. identical header row after normalisation, and identical column count;
3. **no `heading` or `paragraph` element lies between them** — this is the
   condition that protects Exhibit G's ~20 legitimately distinct per-subsection
   tables, which satisfy (1) and (2) but are separated by a `G`-heading;
4. B starts in the upper region of its page and A ends in the lower region —
   a continuation resumes at the top, derived from the page rectangle rather
   than a fixed constant.

The merged element keeps A's `bbox` and page for its anchor, drops B's repeated
header row, and gains `page_span: tuple[int, int]` (see P5). Quality is the
weaker of the two.

### Acceptance

* 42 tables → 34; the Exhibit A matrix is one 20-row element.
* Exhibit G still yields one table per `G` subsection — **not** merged.
* Text conservation ratio stays 1.0000 (the dropped duplicate header is the one
  permitted deletion, and it must be asserted rather than assumed).

---

## P5 — A cross-page merged paragraph cites its first page only

### Evidence

3 paragraphs exceed 900 characters, and the p4 element physically continues onto
page 5. Per-page character accounting shows the distortion: page 4 carries +84%
of its own text and page 5 −60%, purely because merged elements are attributed
to their first page.

`join_wrapped_lines` documents this as intentional — "the merged element keeps
the *first* line's page index, because that is where a reader following the
citation should look". That is right for a paragraph split across a page break.
It is wrong for a compliance citation: if the quoted obligation is physically on
page 5 and the finding cites page 4, the reviewer opens the wrong page. P2 makes
this rarer (fewer merges) but does not remove it.

### Fix

Carry the span rather than a single page. Add to `Element`:

```python
page_end: int | None = None        # last physical page this element's text touches
page_label_end: str = ""
```

`page_index` keeps its meaning (where the element *starts*) so nothing
downstream breaks. `join_wrapped_lines` sets `page_end` when it merges across a
break; P4's stitcher sets it for a spanning table. `Chunk` gains the same pair,
and a citation renders `p.4-5` when they differ.

Better anchoring — mapping a quote's character offset back to the page that
contains it — is possible but needs per-line offsets threaded through the merge.
Deferred: the span is enough to stop a citation being wrong, and the exactness
only matters once generation is wired up (commit 10).

---

## P6 — `_BARE_NUMBER` matches ordinary words

`_BARE_NUMBER = ^[ivxlcdm\d]+$` (case-insensitive) is intended for a page number
or roman numeral. It also matches any word built from those letters:

```
'LLC' → matches      'civil' → matches      'MID' → matches
'mild' → matches     'did'  → matches       'vivid' → matches
```

A block in the header/footer band whose entire text is such a word is dropped as
furniture. **Zero occurrences on this file** — the document has no furniture at
all — so this is latent, not active. It is worth fixing because the failure mode
is silent deletion of content, and `LLC` alone in a band is entirely plausible
in a contract signature block.

**Fix.** Require a digit, or a *well-formed* roman numeral (validated by parsing
it, not by character membership), and require the block to be short. One-line
change in `blocks.py`, no behaviour change on this file.

Related, same area: the `_FOOTER_BAND = 0.85` constant is worth re-deriving.
Body text on this file reaches y=725 of 792 (0.916), well inside the "footer"
band; 76 blocks sit in the bands and every one is real content. Nothing is lost
today only because the second condition (bare number or learned repeating
pattern) never fires. The band should be derived from where content actually
stops — the modal bottom edge of full-width blocks — rather than assumed.

---

## P7 — Dead code from the LaTeX origin

No impact; listed so it is not mistaken for working machinery.

* `_MATH_FONT` / the `equation` class never fires on a Word contract. Keep (it is
  correct for other inputs), but `docs/parsing.md` should stop implying the
  `equation` type is live here.
* `CAPTION_RE` expects `Figure 2.1:` / `Table A.3.`; contract exhibits are
  captioned `Exhibit A — Control Matrix`, which it does not match. Consequence:
  every `TableElement.caption` on this file is empty and table `text` is the
  bare grid. **Do not widen the regex** — the correct fix is P1: the chunker
  prefixes the section breadcrumb, which is what `docs/parsing.md` already
  prescribes and what makes a bare grid embeddable.
* The booktabs recovery rungs (`recovery_clip`, `caption_band`) never fire here.
  Keep — they cost nothing and are the fallback for a non-Word contract.
* `rows_to_markdown`'s `<br>` branch is currently unreachable (P3).

---

## Rejected because they overfit

| Shortcut | Why rejected |
|---|---|
| Split clauses on a literal `^\d{1,2}\.\d{1,2}\s` regex | Fires on `SAML 2.0` and cross-references; 29 mid-prose decimals on this file alone. Corroboration by sequence is what makes it safe. |
| Retune `_MAX_LINE_GAP` / `_INDENT` to Word values | Measured impossible: the gap distributions are inverted and overlapping. Would trade 11 bad merges for more missed ones, and silently break the LaTeX corpus. |
| Repair IDs with a `[A-Z]+-\s\d+` regex | Fixes the 48 IDs and nothing else; leaves `Monitoring/Alertin g` and `just-intime`. |
| Widen `join_lines`' tail to `[A-Za-z0-9]` and stop there | Traced: `gov-01` is attested nowhere, so the test fails and the hyphen is *dropped* — `GOV01`. The naive version of the fix is wrong, which is why the letter→digit rule is structural. |
| Flip the hyphen default to "keep" unconditionally | Correct here (10:0), wrong for LaTeX where typographic hyphens dominate. Measure it per document instead. |
| Hard-code the Exhibit G header to protect it from stitching | The "nothing between them" condition is structural and generalizes to any document with per-subsection tables. |
| Merge tables on matching headers alone | Destroys Exhibit G's ~20 legitimate tables. |
| Widen `CAPTION_RE` to match `Exhibit A —` | Exhibit headings are section headings, not table captions; treating them as captions would duplicate them into table text. P1 solves it properly. |

---

## Commit sequence

Following the repo rule that `plan_implement_docs/` and `tests/` land as their
own commits.

| # | Commit | Contents |
|---|---|---|
| 6a | `docs: parser audit and hardening plan` | this file |
| 6b | `test: parser regression suite on the sample contract` | `tests/test_parse_elements.py`, `tests/test_parse_tables.py`, synthetic-element unit tests plus sample-contract assertions, skipped if the PDF is absent. **Written before the fixes**, so each one is verified by a failing test that then passes. Closes the gap that `parse/` has no tests at all. |
| 6c | `fix(parse): resolve line-break hyphens from measured evidence` | P3 — letter→digit hyphens kept structurally; no-hyphen hard wraps resolved from `profile.words`; `profile.breaks_hyphenate` measured and used as the fallback; vocabulary collector widened to letter-digit compounds; `compact()` takes the profile and splits on `\n`. |
| 6d | `fix(parse): keep numbered clauses as separate elements` | P2 — enumerator lattice with sequence corroboration; `_continues()` veto; indent test gated on measured indentation. |
| 6e | `feat(parse): synthesize section spine from clause numbering` | P1 — `synthesize_spine()` on the 6d lattice; `spine_source`. Replaces planned commit 6. |
| 6f | `fix(parse): stitch tables across page breaks` | P4 — guarded stitcher; `page_span`. |
| 6g | `fix(parse): page spans on merged elements` | P5 — `page_end` / `page_label_end` on `Element` and `Chunk`. |
| 6h | `fix(parse): tighten furniture detection` | P6 — roman-numeral validation; footer band derived from content extent. |
| 6i | `docs: update parsing.md for the hardened parser` | Correct the furniture claim, the equation/caption notes, and the "Measured on the sample contract" table. |

Phase A commits 7-14 renumber accordingly; nothing in them changes.

---

## Verification

A parse is "flawless" on this file when all of the following hold. These become
the assertions of commit 6b.

**Conservation**
- [ ] alphanumeric text ratio `elements+furniture : page` is exactly 1.0000
- [ ] no element has empty text; `order` is `0..n-1`; pages never decrease

**Structure**
- [ ] `spine_source == "headings"`; 145/145 elements have a `section_path`
      (front matter excepted)
- [ ] 41/41 clause boundaries preserved; 0 clauses buried
- [ ] `6.6 Password Management Standard` and `7.2 Data in Transit Requirements`
      are standalone elements with correct breadcrumbs
- [ ] all 51 headings still detected, still no false positives

**Tables**
- [ ] 34 tables; Exhibit A control matrix is one element
- [ ] Exhibit G still one table per `G` subsection
- [ ] 0 corrupted identifiers; all 21 attested control IDs intact
- [ ] 71 wrapped cells repaired, 0 over-joins
- [ ] `just-intime` gone from prose; `breaks_hyphenate is False`
- [ ] every stored grid passes `validate()`

**Citations**
- [ ] every element spanning a page break carries `page_end`
- [ ] no element attributes text to a page it does not touch

**Non-regression**
- [ ] the LaTeX corpus parses with unchanged counts, if it is kept as a fixture
- [ ] parse time stays under 2s for 21 pages

---

## Audit method

Reproducible from a clean checkout; the sample PDF is untracked
(`data/samples/Sample Contract.pdf`, plan commit 13 pending) and the package is
not installed in `.venv`, so:

```bash
PYTHONPATH=src python3 -c "from contract_analyzer.parse import parse_pdf; \
    p = parse_pdf('data/samples/Sample Contract.pdf'); print(p.counts())"
```

The measurements were taken by: comparing whitespace- and
punctuation-stripped page text against element text per page (conservation);
rebuilding the pre-merge paragraph stream and evaluating `_continues()` against
a clause-number oracle (P2); scanning cells for identifier and word-split
patterns and diffing against raw `extract()` output (P3); pairing consecutive
tables on header equality, adjacency and intervening elements (P4); and
per-page character accounting (P5).

Baseline at commit `519cade`: 21 pages, 145 elements — 51 headings, 52
paragraphs, 42 tables, 0 figures, 0 furniture — parsed in 0.95s;
`body_size=12.0`, `body_left=36`, `body_right=577`; `has_outline=False`,
no `/PageLabels`.
