> **SUPERSEDED by `../Front_End_02/`.** Kept as the record of the
> Streamlit attempt and the reasoning that replaced it. The
> post-mortem is reproduced as §1 of `../Front_End_02/02_architecture.md`;
> nothing here is current.

# Front End 01 · the UI specification

**Status: settled 2026-08-24. Unchanged by the move from Streamlit to React
— this was written as a design document, not a framework document, and every
value in it carries over.** The authority on what the UI *is*. How to
build it is `02_react_build.md`; what the API must grow is
`03_api_deltas.md`. Where this document and the prototype disagree, the
prototype (`design/Main.dc.html`) wins — it holds the exact values.

## 1. The shape: scope in the sidebar, views in the tabs

The single organising idea is that **everything is scoped to one document**,
because that is a library invariant, not a UI convention: `retrieve()`,
`chat()` and `analyze_document()` all take a `document_id`, and the API never
passes `ALL_DOCUMENTS`. The navigation makes that visible rather than hiding
it.

* **The sidebar is application navigation.** Upload a contract, browse the
  library, and see the document list. Picking a document here is what sets
  the scope. The active document, its id, page count and chunk count are
  always on screen — the user never has to wonder what they are asking about.
* **The tabs are views of the selected document.** Two of them: **Analysis**
  and **Chat**. They are rendered only on those two views; the Upload and
  Library panes have no tab bar, because they are not views of a document.

An earlier draft put Upload and Library in the tab row alongside Analysis and
Chat. That was wrong: it made four peers out of two app-level pages and two
document-level views, and it left the tab bar showing "Analysis | Chat" while
the user was on a page where neither applied.

The KPI dashboard, when it lands, is a **third sidebar entry**, not a fifth
tab: it is application-level and spans every document.

## 2. Design tokens

Warm neutral paper, ink-on-paper text, a single oxblood accent, and three
status colours that have to survive being the only signal in the room. The
serif is doing real work — it marks the parts that are *quoted from the
contract* as against the parts that are chrome.

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
| Control value, rationale | Source Sans 3 | 14px |
| Meta, secondary | Source Sans 3 | 13px |
| Caption, footnote | Source Sans 3 | 12px |
| Micro label (`.lbl`) | Source Sans 3 | 11px / 700, `0.07em`, uppercase, `#9A9082` |

Both faces are Google Fonts. Fallbacks: `Georgia, serif` and
`system-ui, sans-serif`. **The serif is reserved** for headings, metric
values, and verbatim contract text. Never set UI chrome or an explanation in
it.

### 2.2 Colour

| Token | Value | Used for |
|---|---|---|
| Canvas | `#FAF8F4` | Main background |
| Sidebar | `#F2EEE6` | Sidebar background |
| Surface | `#FFFFFF` | Cards, controls, table rows |
| Surface, selected | `#FBF8F2` | The active library row |
| Nav, selected | `#E5DCCC` | Active sidebar item |
| Border, structural | `#E0D9CC` | Sidebar edge, tab rule |
| Border, card | `#E5DDD0` | Card and table outlines |
| Border, control | `#D6CDBD` | Inputs, buttons, dropdowns |
| Divider, inner | `#EFE9DE` · `#F0EBE1` · `#F3EFE6` | Rules inside a card, lightest last |
| Quote rule | `#C8A88C` | The 3px left rule on every quote |
| Ink | `#23201B` | Primary text |
| Ink, body | `#3A342B` | Rationale, long prose |
| Ink, secondary | `#4A443A` | Button labels |
| Muted | `#6E665A` | Supporting values |
| Meta | `#7C7365` | Page meta, sidebar meta |
| Label | `#9A9082` | `.lbl`, captions |
| Faint | `#A08E7C` · `#A69C8C` · `#B3A896` · `#C4B7A3` · `#DDD5C6` | Icon strokes, disabled, empty-state art |
| **Accent** | `#7A3B2E` | Primary buttons, active tab rule, send, links (`#5C2B21` hover) |
| On accent | `#FDF9F3` | Text and icons on the accent |
| Tooltip | `#2B2721` bg / `#F5F1E9` text | Hover help |

Accent alternates carried in the prototype as a tweak, if the oxblood is ever
rejected: `#2F5D62` (deep teal), `#4A4636` (olive ink), `#1F1B16` (near
black). Changing it changes one token; nothing else in the palette moves.

### 2.3 Compliance state

The three states are the most important thing on the screen and are always
rendered as a chip: 12px/600, `border-radius: 3px`, `padding: 4px 10px`.

| State | Text | Background | Border |
|---|---|---|---|
| Fully Compliant | `#2F6B4F` | `#EEF5F0` | `#B9D3C4` |
| Partially Compliant | `#8A6108` | `#FBF3E3` | `#E4D0A6` |
| Non-Compliant | `#8F2E2E` | `#FAEDEC` | `#E3BFBB` |

Never carry the state in colour alone — the chip always contains the words.

### 2.4 Sub-requirement marker

An 11px square, `border-radius: 2px`, one per sub-requirement, from
`SubRequirementStatus`:

| Status | Marker |
|---|---|
| `met` | solid `#2F6B4F` |
| `partial` | `linear-gradient(135deg, #A9720B 50%, #FFFFFF 50%)`, 1px `#A9720B` border |
| `missing` | white, 1.5px solid `#8F2E2E` |
| `not_determined` | white, 1.5px **dashed** `#B3A896` |

`not_determined` is dashed on purpose: "we could not tell" must not read as
"we checked and it is absent".

### 2.5 Geometry

* Sidebar **336px**, padding `32px 24px`, internal gap 26px.
* Main pane padding `36px 56px 40px`, gap 22px between blocks.
* Radii: `8px` large cards · `6px` cards, controls, buttons · `5px` small
  buttons and nav rows · `3px` state chips · `999px` suggestion chips ·
  `50%` avatars and status dots.
* Icons are **stroke SVG only**, 1.5–2.0 stroke, on a 24px grid, rendered at
  10–34px. No emoji, no dingbats, no icon font. There are eight in the whole
  design; keep it that way.

## 3. The four surfaces

### 3.1 Upload

The drop zone is the whole page until something is uploaded.

* Dashed `2px #D6CDBD` zone, upload-arrow icon, **"Drag and drop a contract
  here"** in the serif, then **"Limit 25 MB per file · PDF only"** — the
  literal `api_max_upload_mb`, so the UI and the `413` agree.
* On success, a result card appears: a green dot, "*filename* is ready", the
  elapsed time, then four values from the `201 Document` response —
  **document id, pages, chunks, outline from** (`spine_source`). Then two
  buttons: **Run compliance analysis** (accent) and **Ask a question
  instead** (secondary).
* Below, permanently, a four-card strip explaining what upload does — parse,
  chunk, index, ready. This is the only explanatory copy in the product, and
  it is here because upload is where a first-time user is most lost. It is
  four sentences; do not let it grow.

The result card is not a toast. It persists, because the document id it
carries is the thing the user needs.

### 3.2 Library

A table, one row per document, on a bordered card.

Columns: **Document** (serif name + "added" timestamp) · **Id** · **Pages** ·
**Chunks** · **Last analysis** · **Actions**.

* "Last analysis" is a chip: `5 of 5 compliant` (green), `2 gaps found`
  (amber), or `Not analysed` (neutral `#7C7365` on `#F4F0E8`).
* Actions are **Analyse**, **Chat**, and a delete icon button. Analyse and
  Chat both set the scope to that row's document and switch to that view.
* The active document's row is tinted `#FBF8F2`.
* A closing sentence under the table states the isolation guarantee in the
  user's terms: *"Each upload becomes its own document id. Retrieval,
  analysis and chat are scoped to one id, so a question about one contract
  can never quote another."*

Deleting needs a confirmation step and an explicit refusal while an analysis
on that document is running (`409 analysis_running`). Neither is designed
yet — see §5.

### 3.3 Analysis

Four states, and the tab must render all four correctly.

**a. No analysis yet.** Centred empty state on a card: document icon, *"…has
not been analysed yet"*, then the sentence that matters — *"A run answers all
five compliance questions against this contract alone. It takes about a
minute and costs roughly a dollar, so it is never started for you."* — and a
**Run compliance analysis** button. Saying the cost out loud is deliberate:
`POST /analyses` refuses duplicate submissions for the same reason.

**b. Queued.** A card: amber dot, "Queued", "1 job ahead of this one", a
**Cancel** button. Stage line reads *"Waiting for a worker — two analyses run
at a time"* (that is `api_workers`). Progress bar at 0. Below, all five
criteria listed as waiting: hollow dot, greyed name (`#A69C8C`), "waiting",
`—` for confidence and latency. A footer row carries elapsed, cost so far,
the pool shape, and the trace id.

**c. Running.** The same card, mutated — never a different layout, so
nothing jumps as it progresses. Green dot, "Analysing five criteria",
"started HH:MM:SS". Stage line names the criterion in flight
(*"criterion 3 of 5 · data_in_transit"*). Progress bar fills. Each finished
criterion turns solid green with its real state, confidence and latency; the
one in flight shows the accent dot and *"retrieving…"*; the rest stay
waiting. Elapsed and cost-so-far tick.

The row list is exactly the `criteria: [{id, status, state?, confidence?}]`
array from `GET /analyses/{id}` — nothing on this screen needs an event
stream, which is why the SSE cut in `05_api_plan.md` costs the UI nothing.

**d. Done.** Header gains **Export JSON** and **Re-run**. Then:

* Four metric tiles: **Overall** (the worst state across the five) ·
  **Mean confidence** · **Quotes verified** (`n / n`) · **Needs review**.
* Five criterion rows, collapsed by default except the first. A collapsed row
  is: chevron, `N · Title` in serif, `k of n met`, `conf 0.95`, state chip.
* Expanded, a row opens to four blocks:
  1. **Sub-requirements** — two-column grid, marker + full requirement text.
     The full text, not the id: a reviewer needs to know what was checked.
  2. **Relevant quotes** — captioned *"showing 2 of 13 — all verified
     verbatim"*, then each quote as serif text in typographic quotes behind a
     3px `#C8A88C` left rule, with `§ ref · p. N · verified` beneath. A
     **Show all N quotes** link.
  3. **Rationale** — 14px, line-height 1.65, capped at `max-width: 900px`.
  4. A footer rule: latency, cost, tool calls, evaluator verdict.

Only one row need be open at a time; opening another closes the first.

**Quote verification is a first-class display concern.** `ResolvedQuote`
carries `verified`, and `ComplianceResult` carries `needs_review`. A quote
that failed verification must not render like one that passed — see §5.

### 3.4 Chat

Top to bottom: settings row, transcript, suggestion chips, input.

**Settings row** — three dropdowns, then a right-aligned line restating the
selection in words (*"Applies to the next question · hybrid retrieval at
medium depth over Sample Contract.pdf"*).

| Control | Options | Default |
|---|---|---|
| **Model** | `claude-opus-5`, `claude-sonnet-5` | `answer_model` |
| **Retrieval** | `hybrid`, `vector`, `keyword` | `retrieval_mode` |
| **Depth** | `shallow`, `medium`, `deep` | `medium` |

Retrieval and Depth carry a hover tooltip on the label; Model does not,
because its options are self-describing. The copy is in the prototype and
should be lifted verbatim.

**Depth is an abstraction over `retrieval_top_k`, and the numbers never reach
the screen.** The mapping is the frontend's, hardcoded, and is the one place
this UI knowingly hides a parameter: a compliance reviewer has no basis for
choosing 4 passages over 8, but does have a basis for choosing "deep" when a
clause is buried in an exhibit. The mapping table lives in
`02_react_build.md` §6.

These are per-question settings, not per-conversation: they apply to the next
question and do not re-run answers already on screen. The line beside them
says so.

**Transcript.** Avatar + block, not bubbles. 30px circle — accent fill with a
person glyph for the user, `#E5DCCC` with a document-check glyph for the
assistant. Then an uppercase role label, then the answer. An assistant answer
is three parts:

1. the prose, 15px, line-height 1.68;
2. **citation cards** — white, `#E5DDD0` border, 3px `#C8A88C` left rule,
   radius `0 6px 6px 0`, serif quote, then `§ ref · p. N · verified`;
3. a usage line: elapsed, cost, tool calls, and *"every quote checked against
   the source passage"*.

**Suggestion chips.** Three pill buttons of real questions about the active
contract. They are a first-run affordance and stay visible; they are cheap
and they teach the user what this thing is for.

**Input.** Bordered row, placeholder *"Ask anything about this contract"*,
accent send button. **Enter sends.** A closing caption states the scope
again: *"Answers are drawn only from …. Nothing outside the active document
is retrieved."*

## 4. Copy rules

* Never say "chunk" to a user except as a count. Say **passage**.
* Never print a raw criterion id (`data_in_transit`) in customer-facing copy
  — except in the running-stage line, where it is the honest name of the
  thing in flight, and in the operator surfaces.
* Costs and durations are stated plainly and in advance. This is a product
  that spends a dollar per click; hiding that is worse than showing it.
* The trace id is always visible on an analysis. It is what makes the log
  walkthrough possible.
* Placeholder facts are bracketed, never invented.

## 5. Not designed yet, and deliberately so

Listed so the implementer does not invent them, and so they can be
prioritised as design work:

1. **Error surfaces.** Nothing renders `no_api_key`, `embedder_unavailable`,
   `ingest_failed`, `payload_too_large`, `unsupported_media_type`, or a
   `409` on delete. Every one of these has a defined `code` and `hint` in the
   API plan; the UI has nowhere to put them. **Highest priority.**
2. **A failed or unverified quote.** `verified: false` and
   `needs_review: true` currently render identically to a clean result. The
   markers exist (§2.4 has the amber and outline treatments) but no screen
   uses them, because the sample contract comes back 23/23 met.
3. **Partially / Non-Compliant results.** Same cause. The chips and markers
   are specified; no artboard exercises them.
4. **Streaming answer text.** The chat streaming state shows the tool trail
   but not an answer typing itself in, which is what `/chat`'s SSE actually
   delivers and the most demo-visible thing in the product.
5. **Citation → source.** A quote card should open the passage it names.
   Today the card is inert. `GET /documents/{id}/sections` exists for it.
6. **Delete confirmation**, and the running-analysis refusal.
7. **Cancel confirmation**, and what the partial report looks like after one.
8. **KPI dashboard.** `KPI_plan.md`. Third sidebar entry.
9. **Responsive behaviour below ~1100px.** The design is specified at 1440;
   the sidebar and the two-column sub-requirement grid are the parts that
   break first.
