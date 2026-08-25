# Generation

What `src/contract_analyzer/generation/` does: one tool-using agent loop,
two finishers. The analysis surface is also three agents around that loop --
Analyzer, Evaluator, Router -- and those have their own folder,
[agents/](agents/README.md). This page is the loop and the finishers; that one
is the conversation between the agents. The model drives retrieval itself; the compliance analysis and
the conversational chat share the loop, the tools, the client, the prompt
library and the caps, and differ only in how the final turn is produced.
Everything below is asserted offline by `tests/test_generation_core.py`,
`tests/test_analysis.py` and `tests/test_chat.py`, which drive the real
`anthropic` SDK over canned SSE through the project's own transport.

## Purpose

```python
from contract_analyzer.compliance import get_criterion
from contract_analyzer.generation import chat, route_criterion

# route_criterion is the Analyzer, the Evaluator and the Router that runs
# them -- see docs/agents/. `analyze_criterion` is still there and is the
# Analyzer alone: it returns the whole run, not just the result.
result = route_criterion(get_criterion("network_auth"), conn, embedder, settings,
                         document_id=doc_id)
result.compliance_state, result.confidence, result.needs_review
result.verdict, result.rounds, result.evaluator_findings
for q in result.relevant_quotes:
    q.text, q.section_ref, q.page_display, q.verified

answer = chat("Does the vendor have to use MFA?", conn, embedder, settings,
              document_id=doc_id, history=previous_turns, on_text=print)
answer.text, answer.citations, answer.evidence, answer.cost_usd
```

## Why two finishers

`output_config.format` (structured output) and document citations **cannot be
requested together -- the API returns a 400.** That one fact shapes the
module:

| | analysis finisher | chat finisher |
|---|---|---|
| Wants | a validated `ComplianceResult` | a streamed answer with verbatim quotes |
| Request | `output_config.format` = the draft schema; citations off; the tool definitions kept with `tool_choice: none` (same `tools → system` prefix as the loop, for caching); the ledger *not* re-sent (it is already in the conversation as tool results) | one document block per ledger entry, citations on, no tools, no `format` |
| Quotes | written by the model, **verified deterministically** against the ledger | **extracted by the API** from the passages sent (`char_location`), so verbatim by construction |
| Effort | `analysis_effort` (medium) | `chat_effort` (low) |
| Model | `analysis_model` | `answer_model` (overridable per question) |

## The loop (`agent.py`)

```
system prompt + task + tool definitions
  └─ while stop_reason == "tool_use":
        execute every tool_use block, append all results in ONE user turn, call again
  └─ finisher(run)
```

`run_agent(task, tools=, finisher=, settings=, client=, on_event=)` returns an
`AgentRun`: the conversation, the evidence ledger, the tool calls, summed
`Usage`, `cost_usd`, `turns`, and `ended_by`:

| `ended_by` | Meaning |
|---|---|
| `model` | the model stopped asking for tools (`end_turn`) |
| `cap` | a counter stopped it: `max_tool_calls`, or the evidence budget |
| `max_tokens`, `refusal` | the API's stop reason, passed through |

**Not getting stuck is enforced by counters, not prompts.** Three of them:

* `CHAT_MAX_TOOL_CALLS` (4) / `ANALYSIS_MAX_TOOL_CALLS` (8 per criterion) --
  tool executions per run. Parallel calls count individually.
* `MAX_EVIDENCE_TOKENS` (12 000) -- the ledger's total. Once reached, a tool
  result says so ("do not search again; answer from the passages you have")
  and the loop stops offering tools.
* **dedupe** -- an identical `(tool, args)` is answered from the ledger at
  zero retrieval cost, so a model that repeats itself runs out of calls
  without burning the index.

Two rules of the loop that are easy to get wrong and fail quietly:

* **The assistant turn goes back wholesale.** `content_params()` echoes
  every block of the response -- thinking blocks with their signatures
  included -- as the next request's assistant message. Rebuilding the turn
  from text + `tool_use` alone would drop blocks the model expects back on
  the same model, and the symptom looks like a model problem, not a bug.
* **All of one turn's tool results go back in one user message.** A response
  may carry several `tool_use` blocks; they are executed in order and their
  `tool_result`s returned together. Splitting them across messages silently
  teaches the model to stop calling in parallel. `max_tool_calls` counts
  *executions*, so three parallel calls in one turn consume three.

On any cap the finisher is still invoked with what exists. A capped run is
never an error; it is a result with `ended_by="cap"`, which the confidence
formula and the KPI page both read.

No request sends a `thinking` parameter. On `claude-opus-5` thinking is on
by default, `budget_tokens` is a 400, and disabling it has documented failure
modes; `output_config.effort` is the one lever, and it is a per-surface
setting. No LangChain / LangGraph: the loop is a `while` with three counters,
and a framework would hide exactly the part walked through in the log during
the demo while bringing a second retry layer against the one-transport rule.

## The tools (`tools.py`)

| Tool | Wraps | The model chooses |
|---|---|---|
| `search_contract` | `retrieve()` | `query`, `mode: hybrid \| vector \| keyword`, `top_k` 1–12 |
| `get_section` | `retrieve_by_section()` | `prefix`: `"6.6"`, `"Exhibit G"` |

`document_id` is bound in Python when `ContractTools` is built. It is not a
parameter, the model never sees it, and no tool call can reach another
contract's text -- `test_no_tool_call_can_reach_a_second_contract` runs the
tools against the two-contract corpus to show it.

The descriptions say *when* each mode wins: keyword for identifiers and exact
jargon (`GOV-01`, `TLS 1.2`, `SAML`), vector for paraphrase ("secure admin
pathway" ≈ "bastion"), hybrid when unsure; `get_section` when a passage
already names the clause to read next.

Bad input is a **result**, never a raise: an empty query, an unknown mode, a
non-integer `top_k`, an unknown tool name all come back as an error result
the model can correct. `vector`/`hybrid` with no embedder returns "call again
with `mode='keyword'`", and the tool description says so up front, so an
offline run degrades to BM25 instead of dying.

### The evidence ledger

Every chunk any tool returns is registered once and given a stable id,
`E1, E2, …`, in first-seen order. A tool result renders the new chunks in
full --

```
[E7] 6. Identity and Access Management > 6.6 Password Management Standard (p.9-10)
Passwords shall be rotated at least every ninety (90) days …
```

-- and the already-seen ones as `already retrieved: E3, E4`. A table's text
is its markdown grid with the breadcrumb in front, as in retrieval. The ledger
is what the analysis finisher's quotes are verified against, what the chat
finisher's document blocks are built from, and what the log records.

## Surface 1: analysis (`analysis.py`)

`analyze_criterion(criterion, conn, embedder, settings, document_id=…)` --
one **Analyzer** run per criterion, returning an `AnalysisOutcome` (the
result, the ledger, the conversation and the live tools). The harness reaches
it through `route_criterion`, which adds the critic and the revise decision;
this section is the run itself. The system prompt carries the criterion, its
sub-requirements and what each status means; the model searches; then:

1. A structured call: `output_config={"format": ComplianceDraft, "effort": …}`.
2. `validate_structure(draft, evidence, criterion)` -- pure Python, see
   [compliance.md](compliance.md).
3. Errors and rounds remain → one user turn listing them
   (`` `relevant_quotes[2].text`: not verbatim in E4 -- copy the exact text ``),
   back to 1. **Feedback says what is malformed, never what the answer
   should be** -- the test asserts the correction turn does not mention the
   state or the quote's content.
4. Errors after `STRUCTURE_FIX_ROUNDS` (2) → every quote that failed is
   dropped (sub-requirement indexes remapped), `needs_review=True`,
   confidence capped at 0.5, `unresolved_errors` kept on the result. A bad
   quote never reaches the UI; a stuck loop never blocks the demo.

Truncation (`max_tokens`) and `refusal` are retried once as plain retries;
twice raises `AnalysisFailed`.

### Confidence

Composed by the Router, not here -- the analyst is one of two estimates that
go into it, and this module no longer has the last word:

```
confidence = min(raw_confidence, critic_confidence)
             × quote_term                                   # critic-judged support
             × (1 − not_determined / total_sub_requirements)
             × (1.0 if the critic agrees on the state else 0.6)
capped at 0.5 when needs_review or ended_by == "cap"
clamped to [0.05, 0.95]
```

A result that never reaches a Router keeps the analyst's own three-term
number, and its `confidence_components` omit the critic keys rather than
inventing agreement. Every term is stored, so a later design can be fitted
without changing the schema.

**It is a heuristic score and it is not calibrated.** The buckets the UI shows
(High ≥ 0.75, Medium ≥ 0.5, Low) are provisional labels, not probabilities.
[agents/confidence.md](agents/confidence.md) says why that distinction is
load-bearing and what it would take to earn the other word.

## Surface 2: chat (`chat.py`, `blocks.py`)

`chat(question, conn, embedder, settings, document_id=…, history=…,
on_text=…)` runs the loop at `CHAT_EFFORT`, then:

* **empty ledger** → `chat.no_context`, no further call. The model has
  already looked; a second request over nothing would invite an answer from
  general knowledge.
* otherwise → one streamed request: a document block per ledger entry, in
  `E` order, `citations: {enabled: true}` on all, the question last.
  `on_text` receives each delta; `get_final_message()` yields the citations
  and usage.

A document block's source is **plain text**, so citations return as
`char_location` -- `cited_text` is what the API extracted from the bytes we
sent, and the offsets index the passage, which is what a UI highlight needs.
Its `title` is the *full* breadcrumb plus page range (retrieval's
`citation_title` is the leaf, because a citation line has a width to fit);
its `context` names the file, the element type, and `sections inferred` when
the breadcrumb was synthesised. `resolve_citations()` maps `document_index`
back to the ledger entry and drops an out-of-range index rather than raising.

History is replayed as **plain text only**, last 8 messages; previous turns'
passages and tool traffic are not re-sent. The current turn re-retrieves, so
"for which accounts?" stays grounded in what it actually found.

## Client, prompts, pricing

* `client.py` -- `anthropic.Anthropic` on `get_http_client()` with
  `max_retries=0`, so the transport's policy is the only retry loop (see
  [http-client.md](http-client.md)). `AnswerUnavailable` before any request
  when there is no key; `AuthenticationError` → `AnswerUnavailable`;
  `APIConnectionError` → the `HttpFailure` it hides; `BadRequestError`
  propagates untouched, because a 400 is a bug in the request we built.
* `prompts.py` / `prompts.json` -- every instruction the model is given, as
  data, validated on load: a missing key fails at startup naming the file and
  the keys it has. Keys: `agent.system` (shared), `chat.system`,
  `chat.no_context`, `analysis.system`, `analysis.user`, `analysis.finish`,
  `analysis.fix_structure`, `analysis.revise` (the Router's findings, as one
  turn), `evaluator.system` (the critic's rubric) and `evaluator.user` (the
  request, as JSON). `PROMPTS_PATH` re-aims the package.
* `pricing.py` -- USD per million tokens for the models this could be pointed
  at, cache reads at 0.1× and writes at 1.25×; an unknown model prices at
  zero and logs `pricing.unknown_model` once.

## What the log shows

Every request is a `span("agent.call", surface, turn, model, effort,
stop_reason, input_tokens, output_tokens, cost_usd, structured)`; every tool
execution a `span("agent.tool", tool, mode, top_k, returned, new, retrieved,
evidence_tokens, note|error)`; a run an `agent.run` span with the counters
and `ended_by`; a criterion an `analysis.criterion` span with state,
confidence and `needs_review`; a correction round an
`analysis.structure_errors` line with the error codes.

Around all of that, the Router's own frames: `router.criterion` (verdict,
rounds, duration, evaluator cost), `router.decision` (verdict, mode, reason
codes), `evaluator.precheck` and `evaluator.critic`, and `analysis.revise`
when a draft goes back. All under one `trace_id`, in `.run/app.jsonl` --
see [agents/](agents/README.md#what-you-can-see-in-the-log).

## Open questions

Prompt caching for the five analysis runs (the shared prefix may cross the
1024-token minimum -- worth measuring in Phase B); refusal fallbacks (skip
until a refusal is observed); the confidence design above.
