# Don't Commit this work in progress

# Step 10b — the Evaluator Agent

**Status: draft, 2026-08-24.** Written after step 10 landed, against the
assignment's §3.2, which requires two agents: a *Router* that decomposes
the question and dispatches retrieval, and an *Evaluator* that assesses
quality, completeness and consistency of the output before it is returned,
with confidence calibration and hallucination detection. This file says
what of that exists, what does not, and the smallest path that closes the
gap without a second orchestration layer.

## What the system does today (after step 10)

```
criterion ──► run_agent ──► tools (search_contract / get_section) ──► evidence ledger
                 │
                 └─► analysis finisher: structured draft ──► validate_structure ──► fix rounds
                                                                     │
                                                                     └─► ComplianceResult
                                                                         (confidence, needs_review)
```

| §3.2 asks for | What exists | Where |
|---|---|---|
| **Router**: decompose the question | sub-requirements authored per criterion; the model judges each | `compliance/criteria.json`, `analysis.system` prompt |
| Router: determine relevant sections/chunks | the model writes queries, picks `keyword\|vector\|hybrid`, follows cross-references with `get_section`; every choice is a logged `agent.tool` span | `generation/tools.py`, `generation/agent.py` |
| Router: dispatch to extractors / table parsers / cross-reference resolvers | one model does extraction inline; tables arrive as grids with breadcrumbs; cross-references are `get_section` calls | `retrieval`, `text_for_model()` |
| **Evaluator**: hallucination detection | **deterministic, quotes only**: every quote verbatim in the ledger passage it names, or dropped + `needs_review` | `compliance/validate.py` |
| Evaluator: consistency | **deterministic, structural only**: state derived from statuses; met/partial needs a quote | `validate.py` |
| Evaluator: completeness | `not_determined` count feeds the confidence; nothing checks whether a `missing` verdict was searched for | `analysis.compute_confidence` |
| Evaluator: quality | — | — |
| Evaluator: confidence calibration | a fixed formula, `raw × verified/claimed × coverage`, capped, clamped; not calibrated against anything | `analysis.compute_confidence` |
| Evaluator: assess *before return* | the structural loop runs before return; no content judgement does | `analysis.finish_analysis` |

**Verdict.** The Router requirement is met in substance: the tool-using
loop *is* the router, and it is a better one than a plan-once router
because each decision is revisable and logged. The Evaluator requirement is
**not** met. What exists is the half of an evaluator that can be written
without a model -- the half that catches a fabricated quote and an
inconsistent state. It cannot catch the failures that matter most on a
contract:

## The gaps, concretely

1. **A verbatim quote that does not support the claim.** "Supplier *may*
   rotate passwords" is verbatim, tagged `E3`, and supports nothing; the
   validator passes it and the state is `met`. This is the hallucination the
   assignment means -- not an invented sentence but a real sentence
   misapplied -- and only a reader can catch it.
2. **`missing` without a search.** A sub-requirement marked `missing`
   after the model searched for something else entirely. The ledger
   records what was retrieved; nothing checks that the retrieval was
   *about* the sub-requirement.
3. **Weak language read as strong.** "Reasonable efforts to", "where
   feasible", "industry standard" marked `met`. The prompt defines
   `partial` for this; nothing verifies the model applied it.
4. **Cross-criterion inconsistency.** Criterion 1 cites §6.6 as requiring
   vaulting; criterion 5 says the contract is silent on privileged
   credentials. Each result is internally consistent; together they are
   not. Runs are per criterion and never see each other.
5. **The confidence number means nothing yet.** It is monotone in the right
   things but has never been compared with a correct answer, so "0.72"
   cannot be read as "72% likely right". The assignment says *calibration*.
6. **No verdict before return.** A result that a critic would send back is
   returned anyway, with a number attached.

## Path forward: one Evaluator, three checks, one verdict

Keep the shape of step 10 -- no framework, one transport, everything a
logged span -- and add **one** model-based component that runs after the
analysis finisher and before the result is returned. It has three parts,
in cost order, and stops early when the cheap part decides.

### E1. Deterministic pre-check (exists; extend)

`validate_structure` as today, plus two cheap additions that need no model:

* **Search coverage.** For every sub-requirement marked `missing`, at
  least one tool call's `query` or `prefix` overlaps its keywords (a token
  overlap test against `SubRequirement.requirement`). Otherwise the
  verdict is `revise` with the instruction *"search for X before marking
  it missing"* -- not a judgement, a fact about the log.
* **Hedge lexicon on `met`.** A quote supporting a `met` status that
  contains "reasonable efforts", "where feasible", "commercially
  reasonable", "may", "industry standard" is flagged for the critic to
  look at specifically. A flag, not an error: sometimes the hedge is in
  a subordinate clause.

### E2. Critic call (new; the Evaluator proper)

One structured call per criterion, on the same client, at
`evaluator_effort` (default `medium`), with **only** the evidence the
result cites plus the result itself -- not the whole conversation. A
critic that sees the analyst's reasoning inherits its errors; one that
sees only quotes and claims has to re-derive the link.

```python
class CriticVerdict(BaseModel):
    quote_support: list[QuoteSupport]      # per quote: supports | partial | irrelevant | contradicts
    status_agreement: list[StatusAgreement] # per sub-requirement: agree | too_strong | too_weak
    state_agreement: Literal["agree", "disagree"]
    missing_searches: list[str]            # sub-requirement ids that deserved another look
    critic_confidence: float               # the critic's own 0-1
    notes: str
```

Rubric in `evaluator.system` (the prompt library already reserves the
`evaluator.*` keys): read each quote as a lawyer would; *obliges* vs
*permits* vs *describes*; a `met` needs an obligation on the vendor with no
material carve-out; `partial` for hedged, scoped or best-efforts language;
say `irrelevant` when the quote is about something else however verbatim.

### E3. Verdict and calibration (new; deterministic)

```
accept   -- no irrelevant/contradicting quote; state agreed; no missing_searches
revise   -- otherwise, and revise rounds remain: one more analysis finisher
            round with the critic's findings as the correction turn
            ("relevant_quotes[1] does not support sub-requirement mfa; search
            for X") -- the same mechanism as the structural fix rounds
fallback -- rounds exhausted: return with needs_review=True, the critic's
            notes attached, confidence capped at 0.5
```

Confidence becomes:

```
confidence = min(raw_confidence, critic_confidence)
             × (supporting_quotes / claimed_quotes)
             × (1 − not_determined / total)
             × (1 if state_agreement == "agree" else 0.6)
capped 0.5 on fallback or ended_by == "cap"; clamped [0.05, 0.95]
```

-- the step-10 formula with the critic's two signals in the two places
they belong: `min()` with the analyst's estimate (two independent
estimates, take the pessimist), and the *supporting* count replacing the
*verbatim* count (verbatim was a proxy for support; the critic measures
support). Components stay on the result.

**Calibration proper** needs labels. The cheapest source is the UI's
reviewer override (accept / change state), stored with the run; a
reliability curve on the KPI page (`confidence bucket` vs `override rate`)
is the calibration evidence, and the bucket thresholds move to match it.
Until there are labels, say so on the KPI page rather than imply
calibration exists.

### E4. Cross-criterion consistency (new; small, deterministic-first)

After all five criteria: quotes citing the same chunk under opposite
statuses (`met` in one criterion, the same chunk absent and the related
sub-requirement `missing` in another) are flagged on the report as
`cross_criterion_notes`. Deterministic pass first; a one-call critic over
the five summaries only if the demo shows the deterministic version
missing things. Do not build the critic version speculatively.

## Do we need more agents?

**No more than one.** The assignment names two; the system has the Router
and needs the Evaluator. Arguments against the others the overall plan
once sketched:

* *Separate extractor agent* -- the analysis finisher already extracts
  from the ledger; a second model doing the same thing from the same
  passages adds a call and a seam, not accuracy.
* *Table-parser agent* -- tables are already grids with breadcrumbs, and
  the pipe-folding in `validate.py` made table quotes verifiable. A model
  re-parsing markdown is worse than the chunker.
* *Cross-reference resolver agent* -- that is `get_section`, called by the
  router when a passage says "see Exhibit G". Making it a separate agent
  would put a model between a string and a SQL prefix match.
* *Orchestrator / supervisor* -- five independent runs in a thread pool
  and a deterministic E4 pass need a function, not an agent.

If one more component is ever justified it is a **self-consistency vote**
(run the analysis finisher `n=3` at higher temperature on a cheaper model
and take agreement as a confidence signal) -- an ensemble, not an agent,
and only if the reliability curve says the critic alone is miscalibrated.

## Where it goes

| File | What |
|---|---|
| `generation/evaluator.py` | `evaluate(result, run, criterion, settings, client) -> (ComplianceResult, CriticVerdict)`; E1 additions, E2 call, E3 verdict and confidence |
| `compliance/schemas.py` | `CriticVerdict`, `QuoteSupport`, `StatusAgreement`; `ComplianceResult` gains `evaluator: CriticVerdict \| None`, `verdict: accept\|revise\|fallback`, `revise_rounds` |
| `generation/prompts.json` | `evaluator.system`, `evaluator.user`; `REQUIRED_KEYS` updated |
| `generation/analysis.py` | after `finish_analysis`: `evaluate()`; on `revise`, one more finisher round with the critic's findings as the correction turn |
| `config.py` | `evaluator_effort` (medium), `evaluator_revise_rounds` (1), `evaluator_model` (defaults to `answer_model`) |
| `compliance/report.py` | `analyze_document()`: five criteria in a `ThreadPoolExecutor`, then E4; `AnalysisReport` |
| `tests/test_evaluator.py` | E1 coverage/hedge rules; a critic verdict of `irrelevant` triggers exactly one revise turn naming the quote; fallback caps confidence; the critic request carries only cited evidence, not the conversation; E4 flags the opposite-status case |

Spans: `evaluator.critic` (tokens, cost, verdict), `evaluator.verdict`
(codes), `analysis.revise` (round). The KPI page gets *evaluator accept
rate* and *revise rate* as tiles; both are the demo's evidence that the
evaluator does something.

Estimated effort: E1 ½ h, E2+E3 2 h, E4 ½ h, tests 1 h, docs ½ h. Cost per
contract: +5 critic calls at medium effort over ~2k tokens each -- roughly
the price of one more analysis run.

## Justification to have ready

*Why is the evaluator a critic call plus rules, not a second analyst?* A
second analyst that sees the same passages makes the same mistakes for the
same reasons. A critic that sees only quotes and claims has to re-derive
whether each quote supports each claim, which is the failure the analyst
cannot see in its own work. The rules around it exist because a model
should never be asked to check what Python can check exactly.

*Why does the router get to decide its own retrieval?* Because the
decision is logged as a tool call with its arguments, revisable on the
next turn, and bounded by counters. A router that plans once in a
separate call is a plan the executor cannot question.
