# Router

`src/contract_analyzer/generation/router.py`. The agent that owns the
conversation between the other two.

```python
from contract_analyzer.generation import route_criterion, cross_criterion_check

result = route_criterion(criterion, conn, embedder, settings, document_id=doc_id)
result.verdict          # accept | fallback | unevaluated
result.rounds           # revision rounds spent
result.evaluator_findings
notes = cross_criterion_check(results)   # after fan-in
```

It owns every decision about **process** and none about **content**.
Retrieval routing belongs to the Analyzer, one logged tool choice at a time;
whether a quote carries its claim belongs to the Evaluator. What sits here is
the layer nothing owned before.

## What it already half-was

Two of the assignment's Router duties were implemented before this file
existed, and stay where they are as authority the Router *delegates*:

* **Question decomposition** -- the sub-requirements are authored per
  criterion in `compliance/criteria.json`; the Router materialises them into
  the task.
* **Determining the relevant sections** -- the Analyzer's tool loop chooses
  queries, retrieval mode and cross-reference follow-ups, each an `agent.tool`
  span. The Router does **not** plan retrieval up front: a plan-once router is
  a plan the executor cannot question, and the loop's per-turn choices are
  revisable and logged.

What is new is the layer above: the Analyzer ↔ Evaluator mediation and the
repeat decision.

## `build_evaluation_request` -- the Router controls what the critic sees

This is where the Evaluator's blindness is *enforced* rather than declared.
The request carries:

* the draft's claims -- state, sub-requirement statuses, quotes, rationale;
* **only the passages the quotes cite**, by evidence id, with section path and
  page;
* the tool-call **queries** the run made, for the search-coverage check.

Not the whole ledger: a passage no claim rests on is not evidence for a claim
nobody made, and offering it invites the critic to write the assessment
instead of check it. Not the conversation, not the thinking, not the tool
results -- `EvaluationRequest` has no field for any of them, so this cannot
silently regress. Queries, never results: what was looked for, not what came
back.

## `decide` -- the policy

| Findings | Rounds left | Decision |
|---|---|---|
| No quote `irrelevant`/`contradicts`, every status agreed, state agreed, `missing_searches` empty | -- | **accept** |
| `missing_searches` non-empty | yes | **revise(research)** -- re-enter the tool loop with `research_extra_tool_calls` |
| Disputed quotes, disputed statuses or state disagreement, nothing unsearched | yes | **revise(redraft)** -- one finisher round on the existing conversation, `tool_choice: none` |
| Anything still open | no | **fallback** -- ship with `needs_review`, findings attached, confidence capped |
| Critic could not be made to answer | -- | **unevaluated** -- the analysis ships flagged; the critic may lower what ships, never block it |

Two things the table is deliberate about:

* **`research` outranks `redraft`** when both apply. A redraft over evidence
  that was never retrieved can only relabel, not learn.
* **`partial` support is not a defect.** It means the contract hedges, which
  is a finding about the contract rather than an error in the assessment.
  Revising on it would cost a round for every hedged clause in the document.

Reason codes (`quote_irrelevant`, `status_too_strong`, `state_disagreement`,
`unsearched_requirement`, `evaluator_failed`) go on the `router.decision` log
line and into the `RevisionRequest`. The instructions that reach the Analyzer
name **what is wrong and where, never what the answer should be** -- the same
contract `analysis.fix_structure` already kept:

```
- relevant_quotes[1] was cited for sub-requirement mfa, and a reviewer reading
  only that passage found it irrelevant: the clause is about physical access
- sub-requirement vaulting was marked missing, but no search in this run went
  looking for it; search for it before concluding it is absent
```

The revise prompt says so explicitly: *"These are findings about your answer,
not the answer: decide for yourself what each one means."* Handing over the
conclusion would make the next draft the Router's, and the Router does not do
content.

## `finalize`

Composes the confidence ([confidence.md](confidence.md)), attaches the
findings **whatever was decided** -- including on `accept`, so the UI can show
why the number is what it is and the KPI page reads agreement rates off stored
results rather than off logs -- sets `verdict` and `rounds`, and folds the
critic's cost into `cost_usd` while keeping it separately visible as
`evaluator_cost_usd`.

Anything but `accept` sets `needs_review`, which caps confidence at 0.5
through the same term a structural failure uses. `verdict` is what lets a
client say *which* of the three it was.

## `cross_criterion_check` -- after fan-in

One inconsistency no single run can see: **criterion A reports no language for
a sub-requirement while criterion B quotes a passage that talks about exactly
that.** The five criteria run in parallel and never meet, so each is right
about what it retrieved and neither the Analyzer nor the Evaluator is in a
position to notice. Only the fan-in is, which makes it a Router duty -- a
process observation across runs, not a content judgement within one.

It compares what makes a sub-requirement different from its siblings (the same
`distinctive_terms` the coverage check uses) against the text of every
verified quote from the other criteria, and writes a note:

```
c1 marked 'vaulting' missing, but c3 quotes Exhibit G, p.12, which mentions
credential, privileg, vault
```

A **note, never an edit**. That two runs disagree is the finding; which one is
wrong is not something this function can know. The notes land in
`AnalysisReport.cross_criterion_notes`, a field that has existed and been
empty since long before this pass, precisely so its arrival would change no
wire format.

A one-call critic over the five summaries is deferred until the deterministic
pass demonstrably misses something.

## Budgets

* `router_max_rounds` (default **1**) -- revisions after the first analysis.
  Zero is legal and means the critic still runs and still lowers the score; it
  just never asks for the answer to be redone.
* `research_extra_tool_calls` (default **3**) -- granted *on top of what the
  first leg spent*. The ledger, dedupe table and token budget carry over, so a
  researching revision cannot re-burn the index.
* Every round's cost accumulates into the result, so the KPI totals stay
  truthful about what the loop actually spent.

## Tested by

`tests/test_router.py` -- the policy table row by row, the isolation asserted
on real outcomes, one disputed quote costing exactly one revision, rounds
exhausted falling back, a critic that fails three times still shipping a
result, and the fan-in fixtures.
