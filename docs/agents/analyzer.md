# Analyzer

`generation/agent.py` (the loop), `generation/tools.py` (retrieval + the
evidence ledger), `generation/analysis.py` (the criterion surface and the
structured finisher), `compliance/validate.py` (the structural gate).

The worker. Given one criterion it searches the contract, extracts evidence,
drafts a structured `ComplianceDraft` and corrects its own **structure** until
`validate_structure` passes or the rounds run out.

Most of it predates the three-agent design and is unchanged by it. What this
page adds is the seam the Router needs.

## The loop, unchanged

`while stop_reason == "tool_use"`, bounded by three counters -- not by
prompting:

* `analysis_max_tool_calls` (8 per criterion);
* `max_evidence_tokens` (12k across the ledger);
* dedupe -- an identical call is answered from the ledger at zero retrieval
  cost, so a model that repeats itself runs out of calls without burning the
  index.

On any cap the finisher runs with what exists and `ended_by` records `cap`
rather than `model`. Every request is an `agent.call` span; every tool
execution an `agent.tool` span.

**This is where the retrieval routing lives, and it stays here.** The model
writes its own queries, picks `keyword` / `vector` / `hybrid`, and follows
cross-references with `get_section`. A plan-once router upstream would be a
plan the executor cannot question.

## The structured finisher, unchanged

Asks for a `ComplianceDraft` through `output_config.format`, with the tool
definitions kept and `tool_choice: none` so the request shares the loop's
exact `tools → system` prefix for caching. The draft is validated in pure
Python against the ledger and the schema's cross-field rules; errors go back
as one user turn for up to `structure_fix_rounds`. Feedback names what is
malformed, never what the answer should be.

When errors survive every round the result is still returned: failed quotes
are dropped, `needs_review` is set, confidence is capped. A bad quote never
reaches the UI; a stuck loop never blocks the demo.

## Retrieved figures are sent as pictures

A figure chunk's text is its caption plus the prose that cites it -- that is
what is embedded, what is rendered and what is cited, and it stays that way.
It is not enough to *read* the diagram, so when a tool result carries a figure
whose asset is on disk, the result is a block list -- the rendered passages,
then the images -- instead of a string, and the chat finisher appends the same
images beside its document blocks (`generation/figures.py`, encoder shared
with the parser in `parse/images.py`). The document blocks themselves stay
plain text, so citations stay `char_location`. Only what a run retrieved is
ever encoded, at most four panels a figure and eight images a request; a
missing or unreadable asset is logged and the caption goes alone. The
ingest-time describer (`--describe-figures`) is a different thing and stays
opt-in and off. `send_figure_images: false` in `settings.json` turns the whole
path off.

## What is new: `AnalysisOutcome`

`analyze_criterion` used to return the result and throw away everything that
produced it. It now returns:

```python
@dataclass
class AnalysisOutcome:
    criterion: Criterion
    result: ComplianceResult
    run: AgentRun          # conversation, ledger, tool calls, usage
    tools: ContractTools   # live, so a revision can search again
    system: str            # the run's system prompt, for cache alignment
    rounds: int = 0
```

Nothing here is recomputed -- it is the run, kept. The Router needs the ledger
to slice cited passages out of, the conversation to continue on a redraft, and
the live tools to search again on a research round.

The finisher also **writes its conversation back to the run**, draft included.
That is what makes a revision continue the real conversation rather than a
reconstruction of it: same prefix, same cache, and the draft being criticised
is actually there.

## What is new: `revise`

```python
outcome = revise(outcome, revision_request, settings=..., client=...)
```

Two modes, both continuing the **same conversation**:

* **`redraft`** -- the findings go in as one user turn and the finisher runs
  again with `tool_choice: none`, same structured format, same validate-and-fix
  machinery. One structured call. Right when the evidence is in hand and the
  reading of it was disputed.
* **`research`** -- the same turn goes in *with tools enabled* and the loop is
  re-entered through `resume_agent`, with `max_tool_calls` raised to
  `already_spent + research_extra_tool_calls`. The counter is **absolute and
  counted against calls already made**, so a revision grants a delta and
  cannot reset the allowance to a fresh eight. Then the finisher runs as
  above.

The ledger, dedupe table and token budget are the same objects in both modes,
so a repeated query is answered from the ledger for free and a researching
revision cannot re-burn the index. `ended_by` reflects the *last* leg;
`result.rounds` is what says a revision happened at all.

## What the Analyzer explicitly does not do

* **Judge its own content.** The fix rounds correct *structure* -- verbatim-ness,
  ids, index ranges, state/status consistency. Whether a verbatim quote
  actually supports the claim is the Evaluator's question, and a second pass by
  the same model over the same conversation would make the same mistake for the
  same reason.
* **Decide to revise.** It never sees `EvaluatorFindings` raw. It sees the
  Router's `RevisionRequest`, which contains only defect-naming instructions.
  Keeping it ignorant of the decision policy means the policy can change
  without touching a prompt.
* **Know about other criteria.** Runs stay per-criterion and parallel;
  cross-criterion consistency is the Router's fan-in pass.

## Spans

`agent.run` (now with `resumed`), `agent.call`, `agent.tool`,
`analysis.criterion`, `analysis.structure_errors`, and the new
`analysis.revise` (mode, round, extra tool calls granted and used). The
`revising` SSE event is emitted from the same place.

## Tested by

`tests/test_analysis.py` (the validator rule by rule, the correction rounds,
the confidence terms), `tests/test_revise.py` (both modes, the conversation
continuing rather than restarting, the budget delta, the ledger identity),
`tests/test_generation_core.py` (the loop and its counters),
`tests/test_figures.py` (a retrieved figure's image on the tool result and
in the chat finisher, and every way a missing asset degrades to text).
