# Step 10 — one tool-using agent loop, two finishers

**Status: implemented 2026-08-24** (`f3272e8`…`4181528`, commits 10a–10k as
sequenced below; 281 tests green, lint clean). Deviations from the plan and
the acceptance state are at the [end](#implementation-report).

Originally: draft for review, revised 2026-08-23. Replaces the earlier draft
of this file. The earlier draft planned generation as "retrieve once, then
answer with citations". Discussion changed the shape: the model now **drives
retrieval itself through tools**, and the same loop serves both the
compliance analysis (Phase B's required deliverable) and the conversational
chat (the bonus). This file is therefore the plan for the shared core *and*
for the two surfaces on it; the earlier `00_overall_plan.md` steps 15–20 are
superseded where they conflict. Steps 11–14 (CLIs, fixture, docs, report)
are unchanged.

## Where we stand

Step 9 (`d40db7d`…`2f6cf79`) put `retrieve()` behind one call: ranked
`RetrievedChunk`s scoped to one contract, each carrying `text_for_model()`,
the breadcrumb, the printed page range and `spine_source`; and
`retrieve_by_section()` for structural lookup. **Nothing reads any of it
yet.** This step is that consumer.

In place already: `Settings.answer_model` (`claude-opus-5`),
`answer_max_tokens`, `answer_effort` (`low`), `anthropic_api_key`,
`prompts_path` → `src/contract_analyzer/generation/prompts.json`, and a
`pyproject` `package-data` line shipping `generation/*.json`. Neither the
directory nor `PromptLibrary` exists; this step writes both.

## Decisions taken in review

1. **The agent retrieves through tools.** Instead of a router that plans
   queries in one call and Python that executes them, the model is given
   `search_contract` and `get_section` and calls them itself: it writes the
   query, picks `vector | keyword | hybrid`, picks `top_k`, and loops until
   it has enough evidence or a hard cap stops it. This is closer to the
   assignment's Router Agent ("determines which sections or chunks are
   relevant, dispatches sub-tasks") than a fixed pipeline, and every decision
   lands in the log as a tool call with its arguments.
2. **One loop, two finishers.** The compliance analysis and the chat share
   the loop, the tools, the client, the prompt library and the caps. They
   differ only in how the final turn is produced, because of the API fact in
   [§2](#2-output_configformat-is-incompatible-with-citations--a-400):
   * *analysis finisher*: `output_config.format` → `ComplianceResult`;
     citations off; quotes are verified deterministically against the
     evidence the tools returned.
   * *chat finisher*: citations on the gathered evidence, streamed; no
     `format`. The quote comes from the API.
3. **Structural self-correction is deterministic, not an LLM.** A validator
   checks the structured output against the evidence and the schema's
   cross-field rules and loops its error messages back to the model, bounded.
   No model judges the *structure*; a model judges only *content*, and that
   is Phase B's evaluator, a separate concern.
4. **Confidence: a deliberately simple first design** — see
   [Confidence](#confidence-first-design-deliberately-simple). To be revisited.
5. **Effort is a per-surface setting.** Analysis and chat carry their own
   `effort`; chat defaults low so a follow-up question does not pay for
   reasoning it does not need.
6. **No LangChain / LangGraph.** The loop is a `while stop_reason ==
   "tool_use"` with three counters. A framework would hide exactly the part
   that has to be walked through in logs during the demo, and would bring a
   second retry layer against the project's one-transport rule.
   `architecture.md` already records this decision.

## What was verified, not recalled

`anthropic` **1.0.0** is installed (the httpx2-based major, which is why the
SDK can be handed our own client at all). A prototype ran the answer path
offline — real SDK, canned SSE through `MockTransport` — so the following
are measurements:

### 1. The quote comes from the API, and it points into our passage

`citations={"enabled": True}` on each document block (all or none). A
**plain-text source** returns `char_location` citations:

```
citation: char_location  document_index=1  chars 120..159
  document_title: 6. Identity … > 6.6 Password Management Standard (p.9-10)
  cited_text:     rotated at least every ninety (90) days
```

`cited_text` is extracted by the API from the bytes we sent, so it cannot be
invented, and the offsets index the exact passage — what a highlight in the
UI will need later.

So: `source={"type": "text", "media_type": "text/plain", "data":
chunk.text_for_model()}`. **Not** the `content` source, whose citations come
back as `content_block_location` — block indexes, coarser, useless for
highlighting.

### 2. `output_config.format` is incompatible with citations — a 400

Structured outputs and citations cannot be requested together. `effort` lives
in the same object and is unaffected.

This is the architectural fact behind decision 2: the analysis and the chat
**cannot** end in the same kind of request. The analysis gets JSON and must
verify its own quotes; the chat gets API-extracted quotes and no JSON.

### 3. Opus 5 thinking is on by default; the cost lever is `effort`

`budget_tokens` is a 400 on this model, and so is an assistant prefill.
Disabling thinking is accepted only at effort ≤ `high` and has two documented
failure modes. So no request sends a `thinking` parameter; `effort` is the
only lever, and it is a setting per surface (decision 5).

### 4. The SDK swallows our `HttpFailure` — and this is the second time

With the retrying transport exhausted, the SDK raises
`anthropic.APIConnectionError("Connection error.")` with `__cause__ =
HttpFailure(...)`. The one-line diagnostic the project promises is inside
`__cause__`. `embeddings/openai.py` already unwraps precisely this. So
`http_client.py` gains `unwrap_http_failure()` and `openai.py` switches to
it. One helper, two callers, no behaviour change.

### 5. The tests can drive the real SDK

Streaming works through `MockTransport` with hand-written SSE: text deltas,
`citations_delta`, `stop_reason`, `usage` all arrive. Tool-use round trips
are the same mechanism (a `tool_use` block in one canned response, the
`tool_result` we send visible in the next request), so the suite exercises
the SDK's own assembly rather than a fake client that agrees with us by
construction.

### Correction carried from the earlier draft

A document block's `title` is the **full** breadcrumb plus printed page range;
step 9's `citation_title` is the **leaf** section because a citation line has
a width to fit. Different on purpose: the full path is what separates
`G1. Governance` from `6.1 Governance` in a contract that has both.

## The shared core

### Tools (`generation/tools.py`)

Thin wrappers over retrieval; nothing new in retrieval itself. `document_id`
is bound on the Python side — the model never sees it and cannot choose it,
so the step-9 scope guarantee holds one layer up.

| Tool | Wraps | Arguments the model chooses |
|---|---|---|
| `search_contract` | `retrieve()` | `query: str`, `mode: vector\|keyword\|hybrid` (default hybrid), `top_k: int` (1–12, default `settings.retrieval_top_k`) |
| `get_section` | `retrieve_by_section()` | `prefix: str` — a clause number or exhibit label, `"6.6"`, `"Exhibit G"` |

Tool descriptions say *when* each mode wins: keyword for identifiers and
exact jargon (`GOV-01`, `TLS 1.2`, `SAML`), vector for paraphrase ("secure
admin pathway" ≈ "bastion"), hybrid when unsure; `get_section` when a passage
already names the clause to read next.

**Evidence ledger.** Every chunk any tool returns is registered once in a
per-run `Evidence` ledger and given a stable id `E1, E2, …`. A tool result is
rendered as the *new* chunks in full (`[E7] {breadcrumb} (p.N)\n{text}`) and
the already-seen ones as ids only (`already retrieved: E3, E4`). The ledger
is what the analysis finisher's quotes are verified against, what the chat
finisher's document blocks are built from, and what the log records.

**Offline path.** `mode="keyword"` works with `embedder=None`; the tool
description says so, and the tool rejects `vector`/`hybrid` with a *result*
("no embedder configured; use keyword") rather than an exception, so an
offline run degrades instead of dying.

### The loop (`generation/agent.py`)

```python
run_agent(task: AgentTask, *, tools, finisher, settings, client, on_event) -> AgentRun
```

* Sends the system prompt, the task's messages and the tool definitions;
  while `stop_reason == "tool_use"`, executes each tool call, appends the
  `tool_result`, and calls again. When the model stops calling tools, the
  `finisher` produces the final turn from the conversation and the ledger.
* **Not getting stuck** is enforced by counters, not prompts:
  * `max_tool_calls` per run (analysis 8 per criterion, chat 4);
  * `max_evidence_tokens` — the ledger's total; once reached, tool results
    say so and the model is told to finish;
  * **dedupe** — an identical `(tool, args)` returns the ledger ids it
    already produced, at zero retrieval cost, so a model that repeats
    itself runs out of calls without burning the index.
  On any cap the finisher is invoked with what exists and the run is marked
  `ended_by="cap"` (vs `"model"`), which the confidence and the KPI page
  both read.
* Every call is a `span("agent.call", surface=…, turn=…)` with model, effort,
  input/output tokens and cost; every tool execution is `span("agent.tool",
  name=…, mode=…, top_k=…, returned=…, new=…)`. Reconstructing the run from
  `.run/app.jsonl` is the demo's live-log walkthrough.
* `AgentRun`: `messages`, `evidence`, `tool_calls`, `usage` (summed),
  `cost_usd`, `ended_by`, `model`, `effort`.

### `generation/client.py`

* `get_client(settings)` → `anthropic.Anthropic(api_key=…,
  http_client=get_http_client(settings), max_retries=0)`.
* `AnswerUnavailable` (mirrors `EmbedderUnavailable`): no key, raised before
  any request, naming `ANTHROPIC_API_KEY` and `.env`.
* Error mapping, nothing more: `AuthenticationError` → `AnswerUnavailable`;
  `APIConnectionError` → `unwrap_http_failure`; `BadRequestError` propagates
  untouched — a 400 is a bug in the request we built.
* `Usage` accumulation and `cost_usd(model, in, out)` from a small pricing
  table in `generation/pricing.py` (the KPI page's cost tile reads this).

### `generation/prompts.py` + `prompts.json`

`PromptLibrary`: loaded once from `settings.prompts_path`, `get(name)`,
`format(name, **kw)`, a `KeyError` that names the file and lists the keys it
has. Flat `{"version": 1, "prompts": {...}}`.

Keys in this step: `agent.system` (shared: you are reading one contract, you
have these tools, search before you answer, prefer the contract's own words),
`chat.system` (appended for chat: answer only from retrieved passages, name
the clause, read a table as a table, say the contract does not appear to
address it rather than reason from general knowledge, no disclaimer),
`chat.no_context`, `analysis.system` (appended for analysis: the criterion,
its sub-requirements, what each state means), `analysis.fix_structure` (the
self-correction turn). Phase B's evaluator adds `evaluator.*` here.

### `generation/blocks.py`

* `document_blocks(chunks)` — one block per ledger entry, in `E` order,
  citations enabled on all; `title` = full breadcrumb + page range;
  `context` = filename, element type, `sections inferred` when
  `spine_source != "outline"`.
* `resolve_citations(message, chunks)` → `[Citation(chunk, quote, start,
  end)]`; an out-of-range `document_index` is dropped, not raised.
* `answer_text(message)`.

### Settings (`config.py`)

| Setting | Default | Used by |
|---|---|---|
| `answer_model` (exists) | `claude-opus-5` | both surfaces |
| `chat_effort` (renames `answer_effort`) | `low` | chat finisher and its loop calls |
| `analysis_effort` (new) | `medium` | analysis loop and finisher |
| `chat_max_tool_calls` | 4 | chat loop |
| `analysis_max_tool_calls` | 8 | analysis loop, per criterion |
| `max_evidence_tokens` | 12 000 | both |
| `structure_fix_rounds` | 2 | analysis finisher |

Chat at `low` is decision 5: a follow-up over five passages is not a
reasoning problem, and the loop's tool calls are the same price whether the
model thinks hard or not. `analysis_effort` is where to spend.

## Surface 1 — compliance analysis (`generation/analysis.py`)

```python
analyze_criterion(criterion, conn, embedder, settings, *, document_id,
                  client=None, on_event=None) -> CriterionRun
```

One `run_agent` per criterion (five per contract; Phase B runs them in a
thread pool and adds the evaluator). The task carries the criterion text and
its sub-requirements; the model searches, then the **analysis finisher**:

1. A call with `output_config={"format": ComplianceDraft, "effort": …}`,
   citations off, tools off, the ledger's chunks *not* re-sent (they are
   already in the conversation as tool results).
2. `validate_structure(draft, evidence) -> list[StructuralError]`. Pure
   Python. `StructuralError(path, code, message)`.
3. If errors and rounds remain: a user turn built from
   `analysis.fix_structure` listing the errors ("`relevant_quotes[2].text`:
   not verbatim in E4 — copy the exact text"), then back to 1. **Feedback
   says what is malformed, never what the answer should be.**
4. If errors persist after `structure_fix_rounds`: drop every quote that
   failed, set `needs_review=True`, cap confidence at 0.5, return. A bad
   quote never reaches the UI; a stuck loop never blocks the demo.

### `ComplianceDraft` / `ComplianceResult` (`compliance/schemas.py`)

The API's constrained decoding guarantees the draft parses: keys, types, the
`ComplianceState` enum. The validator exists for what a schema cannot say:

| Check | Why a schema cannot express it |
|---|---|
| every `quote.evidence_id` is an `E` id in the ledger | runtime cross-reference |
| every quote text is verbatim in that chunk (NFKC, whitespace- and quote-folded, casefold substring) | needs the chunk text |
| `Fully Compliant` ⇒ all sub-requirements `met`; `Non-Compliant` ⇒ none `met`; otherwise `Partially` | cross-field rule |
| a sub-requirement `met`/`partial` has ≥1 quote index; `missing`/`not_determined` has none | cross-field rule |
| `compliance_question` equals the criterion text sent, verbatim | equality to an input |
| no duplicate quotes; quote ≤ 300 chars; rationale non-empty | sanity |

Fields: `compliance_question`, `compliance_state`, `sub_requirements[]`
(`id, requirement, status: met|partial|missing|not_determined, quote_indexes`),
`relevant_quotes[]` (`text, evidence_id` → resolved to `section_ref,
page_display, chunk_id, verified`), `rationale`, `raw_confidence` (0–1, the
model's own estimate, one input to the number below). `ComplianceResult`
adds `confidence`, `needs_review`, `structure_rounds`, `ended_by`,
`usage`, `cost_usd`.

Truncation (`stop_reason == "max_tokens"`) and `refusal` are retried once as
plain retries, not as correction rounds — there is no structure to correct.

### Confidence — first design, deliberately simple

**To be revisited.** What ships now must be explainable in one breath and
must never claim precision it does not have:

```
confidence = raw_confidence
             × (verified_quotes / claimed_quotes)          # 1.0 when no quotes were claimed
             × (1 − not_determined / total_sub_requirements)
capped at 0.5 when needs_review or ended_by == "cap"
clamped to [0.05, 0.95]
```

Three terms, each a sentence: the model's own estimate, cut by the share of
its quotes that were fabricated, cut by the share of the criterion it could
not find language for. The UI shows a bucket — High ≥ 0.75, Medium ≥ 0.5,
Low — with the number beside it; Low is the `needs_review` trigger. The
three components are stored on the result so a later design (critic
agreement, self-consistency voting, reviewer-override calibration) can be
fitted without changing the schema.

## Surface 2 — chat (`generation/chat.py`)

```python
chat(question, conn, embedder=None, settings=None, *, document_id,
     history=(), client=None, on_text=None) -> AnswerResult
```

* `run_agent` with the chat task and caps; `effort=settings.chat_effort`
  throughout.
* **Chat finisher**: if the ledger is empty, `chat.no_context` with no
  further call; otherwise one streamed call with `document_blocks(ledger)`
  and the question, citations on, no tools, no `format`. `on_text` per
  delta; `get_final_message()` for citations and usage.
* **History is replayed as plain text only** — previous turns' passages and
  tool traffic are not re-sent. The current turn re-retrieves through the
  tools, so a follow-up stays grounded. Last 8 messages.
* `AnswerResult`: `text`, `citations`, `evidence`, `tool_calls`, `usage`,
  `cost_usd`, `model`, `stop_reason`.

## Commit sequence

Feature before test, so `make test` is green at every commit. `tests/` and
`plan_implement_docs/` as their own commits, per the repo rule.

| # | Commit | What |
|---|---|---|
| 10a | `refactor(http): one unwrap for the failure the SDKs swallow` | `http_client.py`, `embeddings/openai.py` |
| 10b | `feat(generation): Anthropic client, prompt library, pricing` | `generation/{__init__,client,prompts,pricing}.py`, `prompts.json`; `config.py` settings above |
| 10c | `feat(generation): retrieval tools with an evidence ledger` | `generation/tools.py` |
| 10d | `feat(generation): tool-using agent loop with hard caps` | `generation/agent.py` |
| 10e | `test: client, prompts, tools and the agent loop` | `tests/test_generation_core.py` |
| 10f | `feat(compliance): result schema and structural validator` | `compliance/{schemas,criteria,validate}.py` |
| 10g | `feat(generation): analysis finisher with structural self-correction` | `generation/analysis.py` |
| 10h | `test: analysis -- validator rules, correction rounds, confidence` | `tests/test_analysis.py` |
| 10i | `feat(generation): cited chat over the same loop` | `generation/{blocks,chat}.py` |
| 10j | `test: chat -- request shape, citations, no-context, history` | `tests/test_chat.py` |
| 10k | `docs(generation): one loop, two finishers` | `docs/generation.md`, `docs/compliance.md`; `architecture.md` status lines |

Estimated effort: 10a–10e ~2 h, 10f–10h ~2 h, 10i–10k ~1.5 h. **Cut order
if over budget:** 10i–10j (chat is the bonus; the loop and the analysis are
the deliverable).

## Tests

Canned SSE / JSON through `MockTransport`, driving the real SDK.

**Core (10e):**
* tool definitions carry the mode and `top_k` bounds; `document_id` is not
  a parameter.
* a `search_contract` call reaches `retrieve()` with the model's `mode` and
  `top_k` and the bound `document_id`; results register in the ledger in
  order; a second call returning an overlapping chunk renders it as an id.
* an identical repeated call does not hit retrieval (call counter).
* `vector` with no embedder returns a tool *result* naming keyword, not an
  exception.
* the loop stops at `max_tool_calls` and at `max_evidence_tokens` with
  `ended_by="cap"`, and invokes the finisher once either way.
* no request carries `thinking`; `output_config.effort` equals the surface's
  setting.
* a 401 → `AnswerUnavailable` naming `.env`; an exhausted transport reaches
  the caller as `HttpFailure`, not `APIConnectionError`; a missing key
  raises before any request.
* usage sums across turns; `cost_usd` matches the pricing table.

**Analysis (10f–10h):**
* each validator rule, red and green, on synthetic drafts and a synthetic
  ledger.
* a draft with one non-verbatim quote triggers exactly one correction turn
  whose text names the path and says "not verbatim", and does not mention
  the state or the quote's content; the corrected draft passes.
* errors after `structure_fix_rounds` → quote dropped, `needs_review`,
  confidence ≤ 0.5.
* the finisher request has `output_config.format`, no citations, no tools.
* confidence formula on fixed inputs, including the no-quotes and the
  `ended_by="cap"` cases.
* `compliance_question` paraphrased → structural error.

**Chat (10j):**
* one document block per ledger entry in `E` order, citations on, the
  question last; no `format`.
* `document_index` resolves to the right chunk with page range and
  breadcrumb; `cited_text` verbatim; out-of-range dropped; no citations is
  not an error.
* empty ledger → `chat.no_context`, and the finisher never calls the
  transport.
* history replayed as text only; capped at 8 messages.
* `on_text` deltas concatenate to `AnswerResult.text`.

## Acceptance

- [x] `make test` green, `make lint` clean, no module imports `logging`.
- [x] Every quote in a `ComplianceResult` is verbatim in the ledger chunk it
      names, or the result says `needs_review` — asserted offline.
- [x] Every chat citation's `cited_text` is verbatim in its chunk.
- [ ] With a key: `analyze_criterion` on criterion 5 (auth & authz) over the
      sample contract returns a validated result; the log shows the tool
      calls with their modes and `top_k`, the finisher, and any correction
      round, all under one `trace_id`.
- [ ] With a key: "Does the vendor have to use MFA?" answers from §6.2 with
      `p.4`; "for which accounts?" re-retrieves and stays cited.
- [x] A chat question the contract does not cover returns `chat.no_context`
      after the loop's searches and no finisher call.
- [x] No path can reach a second contract's text.
- [x] `docs/generation.md` records the citations-vs-`format` 400 as the reason
      for two finishers.

## Open questions

1. **Refusal fallbacks** (`client.beta.messages`, `fallbacks: "default"`).
   Recommendation: skip; revisit when a refusal is observed.
2. **Prompt caching.** Recommendation: skip for chat (the stable prefix is
   ~300 tokens, under the 1024 minimum). For analysis the shared system
   prompt plus tool definitions may cross the minimum and the five
   criterion runs share it — worth measuring in Phase B, not deciding now.
3. **Should the loop see the previous chat question when it writes its first
   query?** Cheap to include as context in the task; recommendation: yes,
   the last user turn only.
4. **Confidence** — the design above is a placeholder by intent. Candidates
   for the next iteration, in order of how much they would be trusted:
   evaluator/critic agreement on the state; self-consistency vote share
   (n=3 on a cheaper model); reviewer overrides in the UI as labels for a
   reliability curve on the KPI page.
5. **Per-criterion vs. one run for all five.** One run per criterion keeps
   the ledger small and the caps meaningful, and parallelises; one run for
   all five would share retrieved evidence (6.x serves criteria 1 and 5).
   Recommendation: per criterion, and let Phase B's evaluator notice
   cross-criterion inconsistency if it matters.

## Implementation report

Commits, in the planned order, feature before test, `tests/` and this file
as their own commits:

| # | Commit | Note |
|---|---|---|
| 10a | `f3272e8` refactor(http) | `unwrap_http_failure()` returns the `HttpFailure` or `None`; `openai.py` switched |
| 10b | `49d86e3` feat(generation) | client, prompts, pricing; settings renamed and added |
| 10c | `c827da0` feat(generation) | tools + ledger |
| 10d | `c8a480f` feat(generation) | the loop |
| — | `944b59c` fix(generation) | found by 10e: `span("agent.tool", name=…)` shadowed `span`'s own `name` argument; the attribute is `tool=` |
| 10e | `4015462` test | 32 tests; SSE harness in `conftest.py` |
| 10f | `d27caf3` feat(compliance) | schemas, criteria with sub-requirements, validator |
| 10g | `52dc60b` feat(generation) | analysis finisher, confidence |
| 10h | `b2f82ce` test | 31 tests |
| 10i | `bd66da3` feat(generation) | blocks, chat (amended once: an import cycle `validate → tools → generation/__init__ → analysis → validate`, broken with a `TYPE_CHECKING` import) |
| 10j | `9e53958` test | 9 tests |
| 10k | `4181528` docs | generation.md, compliance.md, configuration, http-client, architecture |

### Deviations from the plan

1. **Sub-requirements are authored, not inferred.** `criteria.json` gains an
   `id` per criterion and a `sub_requirements` list with stable ids split
   from each description's prose. The validator checks the draft's ids
   against them exactly, which is what makes the derived-state rule
   checkable. The plan left where sub-requirements come from unstated.
2. **Two extra prompt keys**: `analysis.user` (the opening user turn) and
   `analysis.finish` (the turn that asks for the structured draft). The plan
   listed only `analysis.fix_structure` for the finisher.
3. **`ToolCall.note` beside `ToolCall.error`.** A dedupe hit or a budget
   refusal is informational (`duplicate`, `budget`) and is *not* sent back
   as `is_error: true`; only bad input is. An error result would push the
   model to retry the same call.
4. **The chat finisher is a fresh request**, not the loop's conversation
   plus blocks: history as text, then one user turn of document blocks and
   the question. Re-sending the tool traffic would double the tokens the
   ledger already costs, and the plan's test ("one document block per
   ledger entry, the question last") is the request shape either way.
5. **Every request is streamed**, loop calls included, via
   `client.messages.stream(...).get_final_message()`. A long structured turn
   then keeps bytes moving under the transport's 60 s read timeout instead
   of racing it; nothing observes the loop's deltas.
6. **Confidence output is rounded to 3 places** and `raw_confidence` is
   clamped into [0, 1] before use (the validator also flags it). Otherwise
   the formula is as planned, and `confidence_components` records the four
   factors including the cap.
7. **Structured-output schema carries no constraints.** No `ge`/`le`/
   `max_length` on `ComplianceDraft` fields: constrained decoding does not
   enforce them, so they live in the validator where a failure gets a name.
8. **`ComplianceResult.unresolved_errors`** keeps the validator messages
   that survived the rounds, so the UI can say *why* a result needs review.

### Open questions, updated

1–2 unchanged (refusal fallbacks skipped; prompt caching to measure in
Phase B). 3: yes — history is replayed as text into the loop's task, so the
first query sees the previous turn. 4: unchanged, placeholder by intent.
5: per criterion, as recommended.

### Not done here

The "with a key" acceptance runs (criterion 5 end to end; the MFA follow-up
pair). They need `ANTHROPIC_API_KEY` and an embedded corpus and cost money;
they belong with the CLIs of steps 11–14, where the log walkthrough is
produced.
