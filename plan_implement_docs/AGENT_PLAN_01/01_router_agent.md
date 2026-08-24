# Router Agent — design

**Status: implemented** in `generation/router.py`. Deviations from this design — `build_evaluation_request` rather than a classmethod, disputed statuses blocking `accept`, and a different (computable) cross-criterion check — are recorded in [06_build_report.md](06_build_report.md) §2, §6, §7.

**Role.** The orchestrator. For each criterion it: builds the task,
invokes the Analyzer, receives the result, packages result-plus-evidence
as JSON for the Evaluator, receives structured findings, decides
(`accept` / `revise` / `fallback`), and repeats the calls when a revision
is warranted. After all criteria it runs the cross-criterion consistency
pass. It owns every decision about *process*; it makes no decision about
*content*.

**File:** `generation/router.py`. Public surface:

```python
def route_criterion(
    criterion: Criterion,
    conn, embedder, settings, *, document_id, client, on_event,
) -> ComplianceResult
    # replaces analyze_criterion as what report.py calls per criterion

def cross_criterion_check(results: list[ComplianceResult]) -> list[str]
    # fills AnalysisReport.cross_criterion_notes (the field already exists)
```

## 1. What the Router already half-is

Two of the assignment's Router duties are **already implemented and stay
where they are**, as authority the Router delegates:

* *Question decomposition* — the sub-requirements are authored per
  criterion in `compliance/criteria.json`; the Router materialises them
  into the task (as `analyze_criterion` does today via
  `criterion.sub_requirements_text()`).
* *Determining relevant sections/chunks* — the Analyzer's tool loop
  chooses queries, retrieval mode and cross-reference follow-ups
  (`ContractTools`), each choice a logged `agent.tool` span. The Router
  does not plan retrieval up front: a plan-once router is a plan the
  executor cannot question, and the loop's per-turn choices are revisable
  and logged. What is new is the layer *above*: the Analyzer ↔ Evaluator
  mediation and the repeat decision, which nothing owns today.

## 2. The loop

```python
outcome = analyzer.run(AnalysisTask(criterion, round=0))          # 02 §3
for round in range(settings.router_max_rounds + 1):
    findings = evaluator.evaluate(EvaluationRequest.build(outcome))  # 03
    decision = decide(findings, round, settings)                  # pure function
    emit / span("router.decision", verdict=..., reasons=..., round=round)
    if decision.verdict == "accept" or decision.verdict == "fallback":
        break
    outcome = analyzer.revise(outcome, RevisionRequest.build(findings, decision.mode))
return finalize(outcome, findings, decision)                      # confidence, fields
```

### `EvaluationRequest.build` — the Router controls the Evaluator's view

The Router extracts from the `AnalysisOutcome`:

* the draft's claims (state, sub-requirement statuses, quotes, rationale);
* **only the evidence passages the quotes cite** (by `evidence_id`, full
  chunk text with section path and page) — never the whole ledger, never
  the Analyzer's conversation or thinking;
* the E1 facts only the Router can see: the run's tool-call queries (for
  the search-coverage check) and which quotes matched the hedge lexicon.

This isolation is a Router responsibility on purpose: an Evaluator handed
the Analyzer's reasoning inherits its errors; one handed quotes and claims
must re-derive the support link. The Router is the component that
guarantees the Evaluator was blind, and the `EvaluationRequest` JSON on
the span is the proof.

### `decide` — the decision policy, deterministic

| Findings | Round budget left | Decision |
|---|---|---|
| No quote `irrelevant`/`contradicts`; state `agree`; `missing_searches` empty | — | **accept** |
| `missing_searches` non-empty | yes | **revise(research)** — re-enter the tool loop with `research_extra_tool_calls` budget and the feedback turn |
| Disputed quotes / too-strong statuses / state `disagree`, nothing unsearched | yes | **revise(redraft)** — one more finisher round on the existing conversation, `tool_choice: none` |
| Anything still open | no | **fallback** — return with `needs_review=True`, findings attached, confidence capped 0.5 |
| Evaluator errored twice | — | **fallback** variant `unevaluated` — the Analyzer's result ships flagged; the Evaluator may lower what ships, never block it |

`research` outranks `redraft` when both apply: a redraft over evidence that
was never retrieved can only relabel, not learn. Reason codes (one per
triggering finding) go on the `router.decision` span and into
`RevisionRequest`, phrased as the structural fix rounds are — *what is
wrong and where, never what the answer should be*:
`"relevant_quotes[1] does not support sub-requirement mfa"`,
`"sub-requirement vaulting was marked missing without a related search;
search for it"`.

### Why the Router is not a model call

Every input to `decide` is already a structured judgement produced by a
model or a rule. A model re-reading that JSON to choose `accept` vs
`revise` would add latency, cost and a new failure mode while being
un-unit-testable; the policy above is a table a test pins. The
intelligence lives at the ends — retrieval in the Analyzer, judgement in
the Evaluator. If a future finding class genuinely needs discretion
(e.g. "is this hedge material?"), the discretion belongs in the
*Evaluator's* rubric, and the Router keeps reading verdicts.

## 3. Budgets and failure strategy

* `router_max_rounds` (default **1**): revisions after the first analysis.
  One round is the honest default — the demo shows the mechanism, and the
  KPI page's revise-rate tells us whether a second round would ever fire.
* `research` mode grants `research_extra_tool_calls` (default **3**) on
  top of the already-spent budget; the ledger, dedupe and
  `max_evidence_tokens` carry over, so a researching revision cannot
  re-burn the index.
* Analyzer failure (`AnalysisFailed`) propagates — nothing to evaluate;
  the harness records the criterion as failed exactly as today.
* Evaluator failure degrades (see table). One retry inside the Evaluator,
  then `unevaluated`.
* Every round's cost accumulates into the result's `usage`/`cost_usd`, so
  the KPI totals stay truthful about what the loop spent.

## 4. Cross-criterion pass (after fan-in)

Deterministic first, exactly as 04_02 E4: quotes citing the same chunk
under opposite readings across criteria (`met` in one; the related
sub-requirement `missing` in another with that chunk never retrieved)
become `cross_criterion_notes` strings on the report — the field already
exists and is empty, so the wire format does not change. A one-call critic
over the five summaries is explicitly deferred until the deterministic
pass demonstrably misses things. This is a Router duty because it is a
process observation across runs, not a content judgement within one.

## 5. Spans and events

| Span | Fields |
|---|---|
| `router.criterion` | criterion, rounds, verdict, evaluator_cost_usd, total_cost_usd |
| `router.decision` | round, verdict, mode, reason codes |
| (emits SSE) | `evaluating`, `revising` (with mode), and the existing `result` event gains `verdict` and `rounds` |

## 6. Tests (`tests/test_router.py`)

* Clean findings → `accept`, zero revise turns, confidence composed with
  the critic's number.
* One `irrelevant` quote → exactly one `revise(redraft)` naming that
  quote; the second findings accepted.
* `missing_searches` → `revise(research)`; the Analyzer re-enters the tool
  loop with the extra budget (scripted transport asserts the extra calls).
* Rounds exhausted → `fallback`: `needs_review=True`, confidence ≤ 0.5,
  findings attached.
* Evaluator raising twice → `unevaluated`, result still returned.
* `EvaluationRequest` contains only cited evidence ids — the isolation
  test, asserted on the request object itself.
* Cross-criterion: the opposite-status fixture yields exactly one note.
