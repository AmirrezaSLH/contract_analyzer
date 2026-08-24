# Analyzer Agent — design

**Role.** The worker. Given one criterion, it searches the contract,
extracts evidence, drafts a structured `ComplianceDraft`, and corrects its
own *structure* until `validate_structure` passes or rounds run out. It is
**built** — this document names what exists, and the one seam it needs so
the Router can re-invoke it with the Evaluator's feedback.

**Files:** `generation/agent.py` (the loop), `generation/tools.py`
(retrieval tools + evidence ledger), `generation/analysis.py` (the
criterion surface and structured finisher), `compliance/validate.py` (the
structural gate it corrects against).

## 1. What exists, unchanged

* **The tool loop** (`run_agent`): `while stop_reason == "tool_use"`,
  bounded by `analysis_max_tool_calls` (8/criterion),
  `max_evidence_tokens` (12k) and dedupe; every request an `agent.call`
  span, every tool execution an `agent.tool` span; `ended_by` records
  whether the model or a counter stopped it. The retrieval-routing
  authority the master plan delegates from the Router lives here.
* **The structured finisher** (`finish_analysis`): asks for a
  `ComplianceDraft` via `output_config.format` with the tool definitions
  kept and `tool_choice: none` (prompt-cache-aligned prefix); validates in
  pure Python; feeds errors back for up to `structure_fix_rounds`;
  truncation/refusal retried once; on exhaustion, failed quotes dropped,
  `needs_review` set, confidence capped. Feedback names what is malformed,
  never what the answer should be — the same contract the Router's
  `RevisionRequest` follows.
* **Result assembly** (`build_result`): quote resolution against the
  ledger, verbatim verification, confidence components.

None of this changes behaviourally. The Analyzer keeps its own model
(`analysis_model`), effort (`analysis_effort`) and counters; the Router
gets no lever inside a round beyond the two entry points below.

## 2. Interface to the Router

```python
@dataclass
class AnalysisTask:            # Router → Analyzer (round 0)
    criterion_id: str
    round: int = 0
    revision: RevisionRequest | None = None   # rounds ≥ 1 only

@dataclass
class AnalysisOutcome:         # Analyzer → Router
    result: ComplianceResult   # as today
    evidence: Evidence         # the ledger — the Router slices cited passages
                               # out of it for the EvaluationRequest
    messages: list[dict]       # the conversation, for redraft rounds
    tools: ContractTools       # live tool state, for research rounds
    system: str                # the run's system prompt (cache alignment)
```

`AnalysisOutcome` is the honest widening of what `analyze_criterion`
already has in scope when it returns; today it throws the conversation and
ledger away. Nothing in it is recomputed — it is the run, kept.

## 3. The one new capability: `revise`

Two modes, chosen by the Router (01 §2), both continuing the **same
conversation** so evidence and prompt cache carry over:

* **`redraft`** — append the `RevisionRequest` findings as one user turn
  (a new prompt key, `analysis.revise`, phrased like
  `analysis.fix_structure`) and run the finisher again:
  `tool_choice: none`, same structured format, same validate-and-fix
  machinery, `structure_fix_rounds` still applying within the round. Cost:
  one-ish structured call.
* **`research`** — append the findings turn *with tools enabled* and
  re-enter `run_agent`'s loop with `max_tool_calls` raised by
  `research_extra_tool_calls` (default 3) over what was already spent.
  Ledger, dedupe and token budget persist, so repeated queries answer from
  the ledger at zero retrieval cost. When the loop stops, the finisher
  runs as in `redraft`. `ended_by` reflects the *last* leg; the result's
  `rounds` field tells the KPI page a revision happened.

Implementation shape: extract the loop body of `run_agent` so it can start
from an existing `(messages, tools)` pair instead of only from a fresh
task; `finish_analysis` already takes `run.messages` and needs only to
accept an optional extra leading turn. Estimated diff: small — the pieces
exist, the seam does not.

## 4. What the Analyzer explicitly does not do

* **Judge its own content.** The finisher's fix rounds correct *structure*
  (verbatim-ness, ids, index ranges, state-status consistency). Whether a
  verbatim quote actually supports the claim is the Evaluator's question —
  a second pass by the same model over the same conversation would make
  the same mistake for the same reason.
* **Decide to revise.** The Analyzer never sees `EvaluatorFindings` raw;
  it sees the Router's `RevisionRequest`, which contains only
  defect-naming instructions. Keeping the Analyzer ignorant of the
  decision policy means the policy can change without touching prompts.
* **Know about other criteria.** Runs stay per-criterion and parallel;
  cross-criterion consistency is the Router's fan-in pass.

## 5. Spans and events

Existing spans unchanged (`agent.run`, `agent.call`, `agent.tool`,
`analysis.criterion`, `analysis.structure_errors`). New:
`analysis.revise` — mode, round, extra tool calls granted/used. The
`structure_errors` SSE event pattern is reused for a `revise` event so the
UI's progress view shows the correction turn happening.

## 6. Tests (extend `tests/test_analysis.py` or new `tests/test_revise.py`)

* `redraft`: a scripted findings turn produces a second draft; the
  conversation grows by exactly two turns; no tool calls made.
* `research`: the revision turn re-offers tools; the scripted transport
  serves one more search; the extra-budget counter caps it; the finisher
  then produces the draft.
* Budget carry-over: a `research` revision after `ended_by="cap"` gets
  only the extra allowance, not a fresh 8.
* `AnalysisOutcome` carries the same ledger object the tools used (
  identity, not copy — the Router's slicing must see verified text).
