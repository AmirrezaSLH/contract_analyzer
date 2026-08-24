# AGENT_PLAN_01 — Master Plan: the three-agent architecture

**Status: implemented, 2026-08-24.** Built in seven commits, `e2e2844` through
`d1f23ed`. What shipped matches this plan except where
[06_build_report.md](06_build_report.md) says otherwise — read that file for
the deviations and why each was made. Steps 7 (the UI half) and the whole of
the confidence plan beyond Phase A are **not** done and are named there.

Supersedes the single-evaluator sketch in
`plan_implement_docs/04_02_evaluator_agent.md` where the two disagree; keeps
its E1–E4 analysis where they do not. The decision this plan implements: the
system has **three named agents** — a **Router** that orchestrates, an
**Analyzer** that does the analysis, and an **Evaluator** that critiques —
with the Router calling the Analyzer, packaging its output as JSON for the
Evaluator, receiving structured feedback, deciding, and repeating the calls
when the feedback says the result is not ready.

Per-agent designs: [01_router_agent.md](01_router_agent.md),
[02_analyzer_agent.md](02_analyzer_agent.md),
[03_evaluator_agent.md](03_evaluator_agent.md). Migration impact:
[04_current_system_changes.md](04_current_system_changes.md).

## 1. The shape

```
                          ┌────────────────────────────────────────────┐
                          │ ROUTER  (generation/router.py)             │
 criterion ─────────────► │  decompose · dispatch · mediate · decide   │ ──► ComplianceResult
                          └───────┬────────────────────────▲───────────┘      (+ verdict,
                                  │ AnalysisTask           │ EvaluatorFindings │  rounds,
                                  │ (+ RevisionRequest     │ (JSON)            │  findings)
                                  │  on later rounds)      │
                          ┌───────▼───────────┐    ┌───────┴───────────┐
                          │ ANALYZER          │    │ EVALUATOR         │
                          │ (analysis.py +    │    │ (evaluator.py)    │
                          │  agent.py, tools) │    │ E1 rules + critic │
                          │ search · extract  │    │ call; sees ONLY   │
                          │ · draft · fix     │    │ quotes and claims │
                          └───────┬───────────┘    └───────▲───────────┘
                                  │ AnalysisOutcome        │ EvaluationRequest
                                  │ (result + evidence     │ (result + cited
                                  │  + conversation)       │  evidence, JSON)
                                  └────────► ROUTER ───────┘
```

Document level: `report.py` remains the **harness** — thread pool, SQLite
connections, event serialisation, the analyses record. It calls the Router
once per criterion instead of calling the Analyzer directly, and calls the
Router's cross-criterion pass after fan-in. The harness is not an agent; it
is plumbing, and keeping it out of the agent count is part of the design's
honesty.

## 2. Who does what

| Agent | Does | Does not | Model call? |
|---|---|---|---|
| **Router** | Builds the per-criterion task from `criteria.json`; invokes the Analyzer; assembles the `EvaluationRequest` (result + only the evidence it cites); invokes the Evaluator; applies the decision policy (`accept` / `revise` / `fallback`); on `revise`, turns findings into a `RevisionRequest` and re-invokes the Analyzer; composes final confidence; runs the cross-criterion pass | Retrieval (delegated to the Analyzer's tool loop), content judgement (delegated to the Evaluator), persistence (harness) | No — a state machine over structured messages. See 01, §"Why the Router is not a model call" |
| **Analyzer** | The existing tool-using loop (`run_agent` + `ContractTools`) and structured finisher (`finish_analysis`): searches, follows cross-references, drafts `ComplianceDraft`, self-corrects structure against `validate_structure` | Deciding whether its own answer is *right*; retrying beyond its counters | Yes — `analysis_model`, `analysis_effort` |
| **Evaluator** | E1 deterministic pre-checks (search coverage, hedge lexicon); E2 critic call over quotes-and-claims only; returns `EvaluatorFindings` | Deciding what happens next (that is the Router's decision policy); seeing the Analyzer's conversation or reasoning | Yes — `evaluator_model`, `evaluator_effort` |

The current codebase already contains the Analyzer whole, the Router's
retrieval half (the tool loop routes its own searches — that authority stays
delegated), and the Evaluator's deterministic pre-history
(`validate_structure`). What is new: `generation/router.py`,
`generation/evaluator.py`, the message schemas, and the revise loop.

## 3. Inter-agent communication protocol

All three agents run **in one process**; the protocol is **typed JSON via
Pydantic models**, passed as function arguments and return values — the same
one-transport rule the rest of the system follows. No queue, no HTTP between
agents, no framework: every message is a Pydantic model that serialises to
JSON, and every hand-off is a logged span, so the demo can show the exact
JSON the Router sent to the Evaluator by grepping one span id in
`.run/app.jsonl`. The messages (full definitions in the per-agent docs):

| Message | From → To | Carries |
|---|---|---|
| `Criterion` (+ `RevisionRequest` later) | Router → Analyzer | what to assess; on rounds ≥ 1, what was wrong with the last attempt. *(No `AnalysisTask` wrapper was built — see 06 §1.)* |
| `AnalysisOutcome` | Analyzer → Router | `ComplianceResult`, the evidence ledger, the conversation handle (for cheap redraft rounds), run bookkeeping |
| `EvaluationRequest` | Router → Evaluator | the result's claims + **only the cited evidence passages** + E1 facts (tool-call queries, hedge flags), round number |
| `EvaluatorFindings` | Evaluator → Router | per-quote support, per-sub-requirement agreement, state agreement, `missing_searches`, `critic_confidence`, notes |
| `RouterDecision` | Router (internal, logged) | `accept` / `revise(mode)` / `fallback`, the reason codes, the round |

Serialising these as JSON is not ceremony: the `EvaluationRequest` JSON is
exactly what an out-of-process evaluator (a different model vendor, a batch
re-scoring job) would receive, so the seam is real even though today it is a
function call.

## 4. The control loop, per criterion

```
round = 0
task  = AnalysisTask(criterion, round=0)
loop:
    outcome  = Analyzer(task)                        # tool loop + finisher (round 0)
                                                     # or redraft/research (round ≥ 1)
    findings = Evaluator(EvaluationRequest(outcome)) # E1 rules, then critic call
    decision = decide(findings, round)               # deterministic policy, one span
    if decision == accept:   return finalize(outcome, findings)   # confidence composed
    if decision == fallback: return finalize(outcome, findings,   # rounds exhausted
                                             needs_review=True, cap=0.5)
    round += 1
    task = AnalysisTask(criterion, round,
                        revision=RevisionRequest(from findings))  # names the defect,
                                                                  # never the answer
```

Decision policy (deterministic — full table in 01):

* **accept** — no quote judged `irrelevant`/`contradicts`, state agreed, no
  `missing_searches`.
* **revise** — otherwise, while `round < router_max_rounds`. Two modes,
  chosen by what the findings say: `redraft` (finisher-only correction turn
  on the existing conversation, `tool_choice: none` — cheap) when quotes or
  statuses are disputed but nothing is unsearched; `research` (re-enter the
  tool loop with a small extra tool-call budget) when `missing_searches` is
  non-empty.
* **fallback** — rounds exhausted with findings still open: return anyway
  with `needs_review=True`, findings attached, confidence capped at 0.5. A
  stuck loop never blocks the demo — the same principle as the structural
  fix rounds today.

Failure strategy: an Evaluator that errors (timeout, refusal, unparseable
findings after one retry) is treated as `fallback`-shaped **degradation, not
failure** — the Analyzer's result returns with `verdict="unevaluated"` and
`needs_review=True`, and the error is on the span. The Evaluator may only
ever *lower* what ships; it must never be the reason nothing ships.

## 5. Confidence

Composed by the Router at finalize, extending `compute_confidence`:

```
confidence = min(raw_confidence, critic_confidence)
             × support_ratio                              # critic-judged support:
                                                          # supports=1, partial=0.5,
                                                          # per (quote, sub-requirement)
             × (1 − not_determined / total)              # coverage, as today
             × (1.0 if state agreed else 0.6)
capped 0.5 on fallback / unevaluated / ended_by == "cap"; clamped [0.05, 0.95]
```

Two independent estimates, take the pessimist; the critic's *support* count
replaces verbatim-ness as the quote term (verbatim was a proxy for support —
the critic measures support; verbatim stays as a hard E-pre gate in
`validate.py`). Components stored on the result, as today. **Calibration
proper still needs labels** — reviewer overrides from the UI remain the
plan, and until they exist the KPI page says "uncalibrated" rather than
implying otherwise (unchanged from 04_02).

## 6. Observability

New spans, all with the run's trace id: `router.criterion` (wraps the whole
loop: rounds, final verdict, total cost), `router.decision` (verdict, reason
codes, round), `evaluator.precheck` (E1 flags), `evaluator.critic` (model,
tokens, cost, agreement counts), `analysis.revise` (mode, round). New SSE
event types mirror them so the UI's progress view can show "evaluating…" and
"revising (research)…" live. KPI tiles: **evaluator accept rate**, **revise
rate**, **verdict-changed-by-revision rate**, evaluator cost share. These
are the demo's evidence that the three agents are real and not a diagram.

## 7. Order of work

| Step | What | Depends on | Est. |
|---|---|---|---|
| ✅ 1 | Schemas: `EvaluatorFindings` + parts, `EvaluationRequest`, `RevisionRequest`; `ComplianceResult` gains `verdict`, `evaluator_findings`, `rounds` (defaults keep old reports parsing) | — | 1 h |
| ✅ 2 | Analyzer seam: `AnalysisOutcome`, `redraft()` and `research()` entry points on the existing conversation/tools (02 §3) | 1 | 1.5 h |
| ✅ 3 | Evaluator: E1 checks, `evaluator.system` / `evaluator.user` prompts, critic call, findings parse + one retry (03) | 1 | 2 h |
| ✅ 4 | Router: `route_criterion`, decision policy, confidence composition, spans (01) | 2, 3 | 1.5 h |
| ✅ 5 | Harness rewire: `report.py` calls the Router; cross-criterion pass fills `cross_criterion_notes` (already on the wire format) | 4 | 1 h |
| ✅ 6 | Config (`evaluator_model/effort`, `router_max_rounds`, `research_extra_tool_calls`), `settings.json`, `REQUIRED_KEYS` | 3, 4 | 0.5 h |
| ◐ 7 | API/SSE events **done**; UI progress states and KPI tiles **not started** | 5 | 1.5 h |
| ✅ 8 | Tests (`tests/test_router.py`, `tests/test_evaluator.py`, extend analysis tests) | 4 | 2 h |
| ✅ 9 | Docs: `docs/agents.md` (the diagram above), README + slide-deck material | all | 1 h |

~12 h total. Cost per contract: +5 critic calls (~2k tokens each at
`evaluator_effort=medium`) plus revise rounds at roughly one extra finisher
call each when triggered — order of one additional analysis run, bounded by
`router_max_rounds`.

## 8. Positions to defend in the interview

* **Why three agents and not one loop?** The Analyzer cannot see its own
  errors — a critic re-deriving quote→claim support from the evidence alone
  catches what a self-check cannot. The Router exists because *someone* has
  to own the conversation between them: which evidence the Evaluator sees,
  when to stop, what a revision may cost. Making that an explicit component
  with logged decisions is what makes the loop auditable.
* **Why is the Router deterministic?** Its inputs are already structured
  judgements. A model interpreting another model's JSON verdict adds a
  failure mode, not intelligence. The intelligence sits at the two ends —
  retrieval routing inside the Analyzer's loop, content judgement in the
  Evaluator — where only a model can do the work.
* **Framework choice** — none, still: the loop above is thirty lines of
  control flow, every hand-off a span. A graph framework would add a second
  retry layer against the one-transport rule and hide the demo's log story.
* **Failure/retry** — counters not prompts (unchanged); the Evaluator
  degrades to `unevaluated` rather than blocking; every retry is bounded
  and lands on the result as a field the KPI page reads.
