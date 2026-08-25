# Front End 02 · the UI specification

**Status: settled 2026-08-24.** The authority on what the UI *is*. This is a
design document, not a React document — it survived one framework change
already and should survive another. Implementation is `02_architecture.md`
onward.

## 1. The shape: scope in the sidebar, views in the tabs

Everything is scoped to one document, because that is a library invariant
rather than a UI convention: `retrieve()`, `chat()` and `analyze_document()`
all take a `document_id`, and the API never passes `ALL_DOCUMENTS`. The
navigation makes that visible.

* **The sidebar is application navigation.** Upload a contract, browse the
  library, and see the document list. Picking a document here sets the scope.
  The active document, its id, page count and chunk count are always on
  screen — the user never has to wonder what they are asking about.
* **The tabs are views of the selected document.** Two: **Analysis** and
  **Chat**. They render only on those views; Upload and Library have no tab
  bar, because they are not views of a document.
* **The URL carries both.** `/upload`, `/library`,
  `/documents/:id/analysis`, `/documents/:id/chat`. A finished analysis is
  linkable, and a reload restores exactly what was on screen.

An earlier draft put Upload and Library in the tab row. That was wrong: it
made four peers out of two app-level pages and two document-level views, and
left a tab bar showing "Analysis | Chat" on pages where neither applied.

The KPI dashboard is application-level — it spans every document — so it sits
outside this navigation entirely, behind an **App / KPI toggle at the top of
the sidebar**, at `/metrics`. In KPI mode the sidebar drops the document scope
for a System block and the two tabs disappear, because no document is in
scope. Designed in `../KPI_01/`. Not a tab, and not a third nav entry.

## 2. Design tokens

Warm neutral paper, ink-on-paper text, one oxblood accent, and three status
colours that must survive being the only signal in the room. The serif does
real work: it marks what is *quoted from the contract* as against what is
chrome.

### 2.1 Type

| Role | Family | Size / weight |
|---|---|---|
| Page title | Source Serif 4 | 30px / 600, `-0.015em` |
| App name (sidebar) | Source Serif 4 | 21px / 600 |
| Card and section title | Source Serif 4 | 19–20px / 600 |
| Criterion row title | Source Serif 4 | 17px / 600 |
| Library document name | Source Serif 4 | 16px / 600 |
| Metric tile value | Source Serif 4 | 20–22px / 600 |
| **Contract quote** | Source Serif 4 | 15px / 400, line-height 1.55 |
| Body, chat answer | Source Sans 3 | 15px / 400, line-height 1.68 |
| Control value, rationale | Source Sans 3 | 14px, rationale line-height 1.65 |
| Meta, secondary | Source Sans 3 | 13px |
| Caption, footnote | Source Sans 3 | 12px |
| Micro label | Source Sans 3 | 11px / 700, `0.07em`, uppercase |

Both faces are Google Fonts; fallbacks `Georgia, serif` and
`system-ui, sans-serif` are real, so a machine with no network gets a
readable page rather than a broken one. **The serif is reserved** for
headings, metric values and verbatim contract text. Never set UI chrome or an
explanation in it.

### 2.2 Colour

| Token | Value | Used for |
|---|---|---|
| `--canvas` | `#FAF8F4` | Main background |
| `--sidebar` | `#F2EEE6` | Sidebar background |
| `--surface` | `#FFFFFF` | **Cards, controls, table rows** |
| `--surface-sel` | `#FBF8F2` | The active library row |
| `--nav-sel` | `#E5DCCC` | Active sidebar item |
| `--border` | `#E0D9CC` | Sidebar edge, tab rule |
| `--border-card` | `#E5DDD0` | Card and table outlines |
| `--border-ctl` | `#D6CDBD` | Inputs, buttons, dropdowns |
| `--divider` | `#EFE9DE` · `#F0EBE1` · `#F3EFE6` | Rules inside a card, lightest last |
| `--rule-quote` | `#C8A88C` | The 3px left rule on every quote |
| `--ink` | `#23201B` | Primary text |
| `--ink-body` | `#3A342B` | Rationale, long prose |
| `--ink-2` | `#4A443A` | Button labels |
| `--muted` | `#6E665A` | Supporting values |
| `--muted-2` | `#787061` | **Meta lines and micro labels — see 2.3** |
| `--hairline` | `#9A9082` | **Non-text only**: icon strokes, rules, empty-state art |
| `--faint` | `#A08E7C` · `#A69C8C` · `#B3A896` · `#C4B7A3` · `#DDD5C6` | Disabled markers, placeholder art |
| `--accent` | `#7A3B2E` | Primary buttons, active tab rule, send, links |
| `--accent-hover` | `#5C2B21` | Link and button hover |
| `--on-accent` | `#FDF9F3` | Text and icons on the accent |
| `--tooltip-bg` / `--tooltip-fg` | `#2B2721` / `#F5F1E9` | Hover help |

Accent alternates, if the oxblood is ever rejected: `#2F5D62` deep teal,
`#4A4636` olive ink, `#1F1B16` near black. The palette pivots on `--accent`
alone; nothing else moves.

**The two-surface rule.** White cards on warm canvas, with the sidebar as a
third, warmer plane, is the whole structural device of this design. If a
surface is a card, it is `--surface` with a `--border-card` outline. This is
stated as a rule because collapsing it is exactly how the previous
implementation failed (`02_architecture.md` §1).

### 2.3 Contrast — a correction

Measured against WCAG 2.1 AA (4.5:1 for text under 18.66px, which is all of
this):

| Token | on `--canvas` | on `--surface` | |
|---|---|---|---|
| `#9A9082` micro label *(old)* | **2.96** | **3.14** | fails |
| `#7C7365` meta *(old)* | **4.40** | 4.67 | fails on canvas |
| `#787061` `--muted-2` *(new)* | 4.62 | 4.90 | passes |
| `#6E665A` `--muted` | 5.33 | 5.66 | passes |

**Micro labels and meta lines both take `--muted-2` (`#787061`).** They were
two different greys; they are now one, and the hierarchy between them is
carried typographically — the label is 11px/700 uppercase at `0.07em`
tracking, the meta line is 13px/400 — which is more differentiation than
0.5:1 of contrast ever gave. `#9A9082` survives as `--hairline` for things
that are not read: icon strokes, rules, empty-state illustration.

The state chips need no change and are recorded so they are not
re-litigated: Fully Compliant **5.68:1**, Partially Compliant **5.02:1**,
Non-Compliant **7.08:1**, each against its own background.

### 2.4 Compliance state

The three states are the most important thing on the screen and are always a
chip: 12px/600, `border-radius: 3px`, `padding: 4px 10px`.

| State | Text | Background | Border |
|---|---|---|---|
| Fully Compliant | `#2F6B4F` | `#EEF5F0` | `#B9D3C4` |
| Partially Compliant | `#8A6108` | `#FBF3E3` | `#E4D0A6` |
| Non-Compliant | `#8F2E2E` | `#FAEDEC` | `#E3BFBB` |
| *(neutral: queued, not analysed)* | `#787061` | `#F4F0E8` | `#E0D9CC` |

**Never carry the state in colour alone.** The chip always contains the
words. A state chip with its text removed is a bug, not a compact variant.

### 2.5 Sub-requirement marker

An 11px square, `border-radius: 2px`, one per sub-requirement, from
`SubRequirementStatus`:

| Status | Marker |
|---|---|
| `met` | solid `#2F6B4F` |
| `partial` | `linear-gradient(135deg, #A9720B 50%, #FFFFFF 50%)`, 1px `#A9720B` border |
| `missing` | white, 1.5px solid `#8F2E2E` |
| `not_determined` | white, 1.5px **dashed** `#B3A896` |

`not_determined` is dashed on purpose: "we could not tell" must not read as
"we checked and it is absent". Shape, not just colour, carries the
distinction — which is also what makes the four legible in greyscale.

### 2.6 Geometry and motion

* Sidebar **336px**, padding `32px 24px`, internal gap 26px.
* Main pane padding `36px 56px 40px`, gap **22px** between blocks, **10px**
  within a group.
* Radii: `8px` large cards · `6px` cards, controls, buttons · `5px` small
  buttons and nav rows · `3px` state chips · `999px` suggestion chips ·
  `50%` avatars and status dots.
* Icons are **stroke SVG only**, 1.5–2.0 stroke, 24px grid, rendered at
  10–34px. Eight in the whole design; keep it that way. No emoji, no icon
  font.
* Two animations exist: the streaming caret and the indeterminate retrieval
  bar. Both must respect `prefers-reduced-motion: reduce` — the caret becomes
  static, the bar becomes a still 30% fill.

## 3. The four surfaces

### 3.1 Upload

The drop zone is the whole page until something is uploaded.

* Dashed `2px --border-ctl` zone, upload-arrow icon, **"Drag and drop a
  contract here"** in the serif, then **"Limit 25 MB per file · PDF only"** —
  the literal `api_max_upload_mb`, read from `GET /health` rather than
  hardcoded, so the UI and the `413` cannot disagree.
* **While uploading**, the zone is replaced by a determinate progress bar
  with the filename and the byte count. A 25 MB PDF over a slow link is
  fifteen seconds of nothing otherwise.
* **On success**, a result card: green dot, "*filename* is ready", elapsed
  time, then four values from `201 Document` — **document id, pages, chunks,
  outline from** (`spine_source`) — and two buttons, **Run compliance
  analysis** (accent) and **Ask a question instead** (secondary).
* Below, permanently, a four-card strip explaining what upload does — parse,
  chunk, index, ready. This is the only explanatory copy in the product, and
  it is here because upload is where a first-time user is most lost. Four
  sentences. Do not let it grow.

The result card is not a toast. It persists, because the document id it
carries is what the user needs next.

### 3.2 Library

A table on a bordered card, one row per document. Columns: **Document**
(serif name + added timestamp) · **Id** · **Pages** · **Chunks** · **Last
analysis** · **Actions**.

* "Last analysis" is a chip: `5 of 5 compliant`, `2 gaps found`, `Not
  analysed`, or the run's status while one is in flight. The UI composes
  those words from `last_analysis.states`, a count per state — which is why
  `05_api_deltas.md` §1 asks for counts rather than a summary string.
* Actions are **Analyse**, **Chat**, and a delete icon button. Analyse and
  Chat both set scope and navigate.
* The active row is tinted `--surface-sel`.
* A closing sentence states the isolation guarantee in the user's terms:
  *"Each upload becomes its own document id. Retrieval, analysis and chat are
  scoped to one id, so a question about one contract can never quote
  another."*

**Delete is a confirmation dialog**, not a one-click action: title *"Delete
*filename*?"*, body naming what goes with it (*"its passages, its search
index and every analysis of it"*), a destructive primary and a cancel. While
an analysis on that document is running, the delete action is disabled with
the reason on hover, and a `409 analysis_running` from a race renders as an
inline error in the dialog rather than closing it.

### 3.3 Analysis

Four states. One card that mutates — never four layouts, so nothing jumps as
a run progresses.

**a. No analysis yet.** Centred empty state: document icon, *"…has not been
analysed yet"*, then the sentence that matters — *"A run answers all five
compliance questions against this contract alone. It takes about a minute and
costs roughly a dollar, so it is never started for you."* — and a **Run
compliance analysis** button. Saying the cost out loud is deliberate;
`POST /analyses` refuses duplicate submissions for the same reason.

**b. Queued.** Neutral dot, "Queued", "1 job ahead of this one", a **Cancel**
button. Stage line: *"Waiting for a worker — two analyses run at a time"*
(that is `api_workers`). Progress at 0. All five criteria listed as waiting:
hollow dot, greyed name, "waiting", `—` for confidence and latency. Footer:
elapsed, cost so far, pool shape, trace id.

**c. Running.** Green dot, "Analysing five criteria", "started HH:MM:SS".
Stage line names the criterion in flight (*"criterion 3 of 5 ·
data_in_transit"*). Progress fills. Each finished criterion turns solid green
with its real state, confidence and latency; the one in flight shows the
accent dot and *"retrieving…"*; the rest stay waiting. Elapsed and cost tick.

The row list is exactly `criteria: [{id, status, state?, confidence?}]` from
`GET /analyses/{id}`. Nothing here needs an event stream, which is why the
SSE cut in `05_api_plan.md` costs this UI nothing.

**d. Done.** Header gains **Export JSON** and **Re-run**. Then:

* Four metric tiles, **all four the same object**: **Overall** (worst state
  across the five, as a chip) · **Mean confidence** · **Quotes verified**
  (`n / n`) · **Needs review**.
* Five criterion rows, collapsed except the first. **A collapsed row carries
  all four of its data points**: chevron, `N · Title` in serif, `k of n met`,
  `conf 0.95`, state chip, right-aligned. This row is the primary scanning
  surface of the product — it is where a reviewer decides what to open — and
  demoting any of it to the inside of the row is a functional regression, not
  a cosmetic one.
* Expanded, a row opens to four blocks:
  1. **Sub-requirements** — two-column grid, marker + **full requirement
     text**. The text, not the id: `GOV-04` does not say what was checked.
  2. **Relevant quotes** — captioned *"showing 2 of 13 — all verified
     verbatim"*, then each quote as serif text in typographic quotes behind a
     3px `--rule-quote` left rule, with `§ ref · p. N · verified` beneath.
     A **Show all N quotes** disclosure.
  3. **Rationale** — 14px, line-height 1.65, `max-width: 900px`.
  4. Footer rule: latency, cost, tool calls, evaluator verdict.

Opening a row closes the previously open one.

**e. Failed and cancelled.** `failed` renders the runner's `error` string
verbatim under a Non-Compliant-toned banner, with **Re-run**. `cancelled`
renders the partial report — the criteria that finished — behind a neutral
banner saying how many of five completed. Neither is a dead end.

### 3.4 Chat

Settings row, transcript, suggestion chips, input.

**Settings row.** Three dropdowns, then a right-aligned line restating the
selection in words: *"Applies to the next question · hybrid retrieval at
medium depth over Sample Contract.pdf"*.

| Control | Options | Default |
|---|---|---|
| **Model** | `claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5` | `answer_model` |
| **Retrieval** | `hybrid`, `vector`, `keyword` | `retrieval_mode` |
| **Depth** | `shallow`, `medium`, `deep` | `medium` |

Retrieval and Depth carry a hover tooltip on the label; Model does not,
because its options are self-describing. Tooltip copy is in the prototype and
should be lifted verbatim.

**Depth is an abstraction over `retrieval_top_k`, and the numbers never reach
the screen.** The mapping is the frontend's and is hardcoded. This is the one
place the UI knowingly hides a parameter: a compliance reviewer has no basis
for choosing 4 passages over 8, but does have a basis for choosing "deep"
when a clause is buried in an exhibit. `medium` must equal
`settings.retrieval_top_k`. The shallow and deep values are **not yet
settled** — they need a recall measurement against the five criteria, and
until then `{shallow: 3, medium: 6, deep: 12}` is a placeholder that must be
labelled as one in the code.

These are per-question settings, not per-conversation: they apply to the next
question and do not re-run answers already on screen. The line beside them
says so.

**Transcript.** Avatar + block, **not bubbles**. 30px circle — accent fill
with a person glyph for the user, `--nav-sel` with a document-check glyph for
the assistant. Then an uppercase role label, then the answer, which is three
parts:

1. the prose, 15px, line-height 1.68, **streaming token by token** with a
   blinking caret while it arrives;
2. **citation cards** — `--surface`, `--border-card`, 3px `--rule-quote` left
   rule, radius `0 6px 6px 0`, serif quote, then `§ ref · p. N · verified`;
3. a usage line: elapsed, cost, model, tool calls, and *"every quote checked
   against the source passage"*.

While retrieving, the assistant block shows the tool trail behind a
`--border` left rule — *"searching …, hybrid retrieval, top 6 passages"*,
*"reading § 6.7 …"* — and an indeterminate bar. It is replaced by the answer,
not appended to.

**Suggestion chips.** Three pill buttons of real questions about the active
contract. A first-run affordance that stays visible; cheap, and they teach
the user what this is for.

**Input.** Bordered row, placeholder *"Ask anything about this contract"*,
accent send button. **Enter sends**; the field clears; an empty input does
nothing. Closing caption: *"Answers are drawn only from …. Nothing outside
the active document is retrieved."*

## 4. Error surfaces

Every `code` the API defines has exactly one place it renders. This table is
the specification; there is no generic error toast in this product.

| Code | Where | What it says |
|---|---|---|
| `unsupported_media_type` | Inline, under the drop zone | "That is not a PDF. This reads contracts as PDF only." — zone stays, no upload attempted |
| `payload_too_large` | Inline, under the drop zone | "*filename* is *N* MB. The limit is 25 MB." |
| `embedder_unavailable` | Replaces the upload result card | "The document could not be indexed: the embedding service is unavailable." + the `hint`, + **Try again** |
| `ingest_failed` | Replaces the upload result card | The API's `message` verbatim — it names the failure — + **Try again** |
| `no_api_key` | Blocking banner on Analysis and Chat, above the tab bar | "No answer model is configured, so analysis and chat are unavailable." + `hint`. The **Run** button is disabled with the same reason on hover; upload still works, and the banner says so |
| `document_not_found` | Full-pane empty state on `/documents/:id/*` | "That document is no longer in the library." + **Back to library** |
| `analysis_running` | Inline in the delete dialog | "An analysis of this contract is running. Cancel it first, or wait." |
| `validation` | Inline at the control that caused it | The `message`; should be unreachable from the UI and is a bug if seen |
| *network / 5xx* | Inline where the data would have been | "Could not reach the analyzer." + **Retry**. Never a blank pane |

Three rules that hold across all of them:

1. **The `hint` is the second line, always**, and it is written for a person
   (`05_api_deltas.md` §4).
2. **An error never destroys work.** A failed question keeps its text in the
   input; a failed analysis keeps the previous report on screen.
3. **No spinner survives an error.** The single failure a live demo cannot
   recover from is a spinner that never resolves.

## 5. The states the sample contract never shows

The sample comes back 23/23 met, so the design has only ever been seen green.
These are specified so they are built, not discovered:

* **A `Partially Compliant` or `Non-Compliant` criterion.** Chip in the row
  and in the Overall tile; `partial` and `missing` markers in the
  sub-requirement grid. The row is otherwise identical — the design must not
  editorialise about a failing criterion beyond stating it.
* **`verified: false` on a quote.** The meta line replaces "verified" with
  **"not found verbatim — check the source"** in `#8A6108`, and the card's
  left rule takes the same amber. This is the single most important state in
  the product: an unverified quote is the hallucination-detection story made
  visible, and it must not look like a clean quote.
* **`needs_review: true` on a result.** A marker in the collapsed row, beside
  the confidence, and a line at the top of the expanded body naming why —
  from `unresolved_errors` when it is non-empty.
* **`not_determined` sub-requirements**, with the dashed marker.
* **A cancelled run's partial report** (§3.3e).

Build these against a fixture, not against the sample. `06_build_and_ship.md`
§5 makes a gap fixture part of commit 13e.

## 6. Copy rules

* Never say "chunk" to a user except as a count. Say **passage**.
* Never print a raw criterion id in customer-facing copy — except the
  running-stage line, where it is the honest name of the thing in flight.
* Costs and durations are stated plainly and in advance. This product spends
  a dollar per click; hiding that is worse than showing it.
* The trace id is always visible on an analysis. It is what makes the log
  walkthrough possible.
* Placeholder facts are bracketed, never invented.

## 7. Accessibility

Not a separate pass; these are part of "done".

* **Contrast** per §2.3. Re-measure any new token against `--canvas` *and*
  `--surface` before using it.
* **Status is never colour alone** — §2.4 and §2.5 both carry shape or words.
* **Keyboard**: every control reachable and operable; the three dropdowns
  open on Enter/Space, move on arrows, close on Escape; the delete dialog
  traps focus and restores it on close; criterion rows are `<button>`-backed
  disclosures with `aria-expanded`.
* **Live regions**: the analysis stage line and the streaming answer are
  `aria-live="polite"` so a screen reader hears progress rather than
  silence.
* **Motion** per §2.6.
* **The transcript is a list of articles**, not a `<div>` soup — each turn is
  addressable.

## 8. Deliberately out of scope

1. **KPI dashboard.** Designed in `../KPI_01/`; metric selection is
   `KPI_plan.md`'s. Reached by the sidebar toggle, at `/metrics`.
2. **Citation → source.** A quote card should open the passage it names;
   `GET /documents/{id}/sections` exists for it. Today the card is inert.
   The highest-value thing to add after this plan ships.
3. **Re-run history.** `GET /analyses?document_id=` returns every run; the UI
   shows the newest.
4. **Multi-document comparison**, and anything that would need
   `ALL_DOCUMENTS`.
5. **Responsive below ~1100px.** Specified at 1440. The sidebar and the
   two-column sub-requirement grid break first; a tablet pass is a later
   commit, and the token set does not change for it.
