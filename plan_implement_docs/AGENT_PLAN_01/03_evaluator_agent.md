# Evaluator Agent — design

**Status: implemented** in `generation/evaluator.py`. Two deviations worth knowing about: the search-coverage check compares *distinctive* terms rather than any token overlap (the plain version could never fire), and the retry ladder is three attempts with exponential backoff rather than one immediate retry. See [06_build_report.md](06_build_report.md) §3, §5.

**Role.** The critic. Given a JSON `EvaluationRequest` from the Router —
the Analyzer's claims plus only the evidence those claims cite — it judges
whether each quote supports its claim, whether each status and the overall
state follow, and whether `missing` verdicts were actually searched for.
It returns structured `EvaluatorFindings` and **nothing else**: no verdict
on what happens next (Router), no rewriting of the answer (Analyzer).

**File:** `generation/evaluator.py`. Public surface:

```python
def evaluate(request: EvaluationRequest, settings, client) -> EvaluatorFindings
```

Carries forward E1/E2 from `04_02_evaluator_agent.md`; E3's verdict logic
moves to the Router (01 §2), E4's cross-criterion pass likewise (01 §4).

## 1. Why the Evaluator sees only quotes and claims

The one failure the Analyzer structurally cannot catch is a **verbatim
quote that does not support the claim** — "Supplier *may* rotate
passwords" is verbatim, passes `validate_structure`, and supports nothing.
A critic shown the Analyzer's conversation inherits the reasoning that
made the mistake; a critic shown only the quote, the cited passage and the
claim has to re-derive the support link, which is exactly the check
missing today. The Router enforces this blindness by construction
(01 §2); the Evaluator's request schema makes it impossible to violate
silently.

## 2. E1 — deterministic pre-checks (no model)

Run first, cheap, results attached to the request as facts for the critic
and to the findings as flags:

* **Search coverage.** For each sub-requirement marked `missing`: does any
  tool call's `query`/`prefix` (provided by the Router from the run's
  `tool_calls`) token-overlap the sub-requirement's text? A miss is a fact
  about the log, not a judgement — it feeds `missing_searches` directly
  and is reported even if the critic call fails.
* **Hedge lexicon on `met`.** Quotes supporting a `met` status containing
  "reasonable efforts", "where feasible", "commercially reasonable",
  "may", "industry standard", "endeavour" are flagged for the critic to
  examine specifically — a flag, not an error; sometimes the hedge is in a
  subordinate clause and only a reader can tell.

Python checks what Python can check exactly; the model is asked only what
needs reading.

## 3. E2 — the critic call

One structured call per evaluation, on the same client and transport:
`evaluator_model` (default: `analysis_model`), `evaluator_effort`
(default `medium`), `output_config.format` on the findings schema, no
tools. Truncation/refusal/parse failure retried once; a second failure
raises, which the Router degrades to `unevaluated` — the Evaluator can
lower what ships, never block it.

### Schemas (`compliance/schemas.py`)

```python
class QuoteSupport(_Strict):
    quote_index: int
    sub_requirement_id: str
    support: Literal["supports", "partial", "irrelevant", "contradicts"]
    note: str                      # one sentence; shown in the UI on demand

class StatusAgreement(_Strict):
    sub_requirement_id: str
    agreement: Literal["agree", "too_strong", "too_weak"]
    note: str

class EvaluatorFindings(_Strict):
    quote_support: list[QuoteSupport]
    status_agreement: list[StatusAgreement]
    state_agreement: Literal["agree", "disagree"]
    missing_searches: list[str]    # sub-requirement ids; union of E1 facts
                                   # and the critic's own judgement
    critic_confidence: float       # the critic's independent 0–1
    notes: str
```

`EvaluationRequest` (built by the Router, 01 §2) carries: criterion id and
sub-requirements; the draft's state, statuses, quotes and rationale; the
cited passages as `{evidence_id, section_path, page, text}`; the E1 flags;
the round number. Its JSON serialisation is the out-of-process seam: a
batch re-scorer or second-vendor critic consumes exactly this object.

### Rubric (`evaluator.system`, `evaluator.user`)

Read each quote as a lawyer would. Distinguish **obliges** ("shall",
"must") from **permits** ("may") from **describes** ("currently uses").
A `met` needs an obligation on the vendor with no material carve-out;
hedged, scoped or best-efforts language is at most `partial`; say
`irrelevant` when the quote is about something else, however verbatim.
Judge only against the passages provided — the critic has no retrieval and
must not import outside knowledge of "what contracts usually say". The
prompt library already reserves the `evaluator.*` keys; add both to
`REQUIRED_KEYS`.

Structural sanity of the findings themselves (indexes in range, known
sub-requirement ids) is checked in Python after parsing; a violation
counts as the one retry.

## 4. What the Evaluator returns is evidence, not authority

The findings land on the final result (`ComplianceResult.evaluator_findings`)
whatever the Router decides, including on `accept` — the UI can show *why*
confidence is what it is, and the KPI page computes agreement rates from
stored results rather than from logs. `critic_confidence` enters the
confidence formula via `min()` with the analyst's estimate, and the
critic-judged `supports` count replaces verbatim count in the quote term
(master plan §5); verbatim-ness remains a hard gate upstream in
`validate.py`.

## 5. Cost and spans

Per contract: 5 critic calls at ~2k tokens each, `medium` effort — roughly
one extra analysis run; each revise round adds one more evaluation of the
revised result. Spans: `evaluator.precheck` (flags found),
`evaluator.critic` (model, tokens, cost_usd, counts of
irrelevant/contradicts/too_strong, state_agreement, critic_confidence).
Both spans, like everything else, carry the trace id — the demo can show
the critic disagreeing in `.run/app.jsonl` next to the analysis turn it
disagreed with.

## 6. Tests (`tests/test_evaluator.py`)

* E1 coverage: `missing` with no overlapping search → the id appears in
  `missing_searches` even when the scripted critic omits it.
* E1 hedge: "commercially reasonable efforts" quote under a `met` status →
  flagged in the request the critic receives.
* The request contains only cited evidence and no conversation — asserted
  on the built `EvaluationRequest`.
* A scripted `irrelevant` finding round-trips into the stored result.
* Parse failure then success → one retry, one span pair; double failure →
  raises (the Router test covers the degradation).
* Findings with an out-of-range `quote_index` → counted as the retry.
