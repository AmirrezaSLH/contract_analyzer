# Build report — what shipped, and where it left the plan

**Status: 2026-08-24.** Seven commits, `e2e2844` → `d1f23ed`, tree green
throughout (402 passed, 50 skipped; baseline was 335/50, so this pass added
67 tests). Read alongside [00_master_plan.md](00_master_plan.md); this file is
only the delta.

| Commit | Step | What |
|---|---|---|
| `e2e2844` | 1 | the protocol schemas |
| `fd049b2` | 2, part of 6 | `AnalysisOutcome`, `revise`, `resume_agent`, `analysis.revise` |
| `9d6ea0d` | 3, part of 6 | the Evaluator, its prompts, its settings |
| `fd0bc18` | 4 | the Router, the decision policy, the confidence composition |
| `3aeed91` | 5 | the harness rewire, totals, the analyses columns |
| `1cebf79` | 7 (API half) | SSE events, widened schemas, regenerated types |
| `d1f23ed` | 9 | `docs/agents/`, and the docs the change made untrue |

## What is *not* done

* **Step 7's UI half.** The `evaluating` / `revising` / `decision` events reach
  a subscriber and are tested; nothing in `ui/` reads them yet. No verdict
  badge, no findings drawer, no KPI tiles. The front end builds unchanged
  against the widened types, which is what "extend, don't rename" bought.
* **Everything in [05_confidence_plan.md](05_confidence_plan.md) past Phase A.**
  The formula and the honest naming shipped. Wilson intervals, reviewer
  overrides, the gold set, the reliability curve and the fitted mapping did
  not. `docs/agents/confidence.md` states the gate for each.

## Deviations from the plan, and why

### 1. No `AnalysisTask` type

**Plan** (02 §2) named a `AnalysisTask(criterion_id, round, revision)` message
for Router → Analyzer. **Built**: the Router passes the `Criterion` itself on
round 0 and a `RevisionRequest` on later rounds.

The wrapper would have re-boxed two things the call already carries and
collided by name with `agent.AgentTask`. An indirection that exists only to
make a table in a design doc symmetrical is ceremony, and the protocol table
in `docs/agents/` names what actually crosses the seam instead.

### 2. `EvaluationRequest.build` is `router.build_evaluation_request`

**Plan** (01 §2) put a `build` classmethod on the schema. **Built**: a
function in `router.py`.

`compliance/schemas.py` would have had to import `generation.tools` to read
an `AnalysisOutcome`, and `generation` already imports `compliance` — the
classmethod was a circular import wearing a nicer name. Keeping the builder in
the Router is also more honest about whose responsibility the isolation is.

### 3. Search coverage compares *distinctive* terms, not any overlap

**Plan** (03 §2) said "does any tool call's query token-overlap the
sub-requirement's text?" **Built**: overlap against what separates a
sub-requirement from its siblings, over a four-rule stemmer.

The plan's version does not work, and it took ten minutes with the real
criteria to find out. Every sub-requirement of a password criterion contains
"password", so *any* query would have covered all five and the check could
never fire — a flag that never fires is worse than no flag, because it looks
like coverage. Subtracting the criterion's requirement text and the sibling
sub-requirements leaves "vaulting", "privileged": the words a search would
have had to go after to have looked for this one specifically. The stemmer
exists because "rotated" and "rotation" are the same word and calling that
unsearched would be a false accusation dressed as a fact about the log.

### 4. `partial` support scores a half, and is not a defect

**Plan** (§5) said `supporting_quotes / claimed_quotes`. **Built**:
`supports` = 1.0, `partial` = 0.5, `irrelevant`/`contradicts` = 0.0, over one
judgement per *(quote, sub-requirement)* pair.

Scoring `partial` at zero punishes an analyst who correctly answered `partial`
with correctly partial language — the contract hedged, and the assessment said
so. For the same reason `partial` does not trigger a revision: it is a finding
about the contract, not an error in the assessment, and revising on it would
cost a round for every hedged clause in the document.

The per-pair unit is a correction the plan did not specify: a quote cited for
two sub-requirements is two claims and is judged twice.

### 5. The retry ladder is three attempts with backoff, not one immediate retry

**Plan** (03 §2) said "retried once". **Built**: three attempts total, with
full-jitter exponential backoff between them, base 0.5 s.

The transport already retries connection failures, timeouts, 429s and 5xxs
with exponential backoff — one retry loop for the process, and `evaluator.py`
deliberately does not run a second policy against the same server (a test pins
that four transport attempts do not become twelve). What the ladder here
retries is *semantic* failure, and the load-shaped ones — truncation, refusal
— clear when the load does. Worst case ~1.5 s of waiting against the
alternative of shipping a criterion nobody criticised.

### 6. A disputed *status* triggers a revision

**Plan** (01 §2) named "too-strong statuses" in the revise row but left them
out of the accept row. **Built**: `accept` requires no disputed quotes, **no
disputed statuses**, state agreement, and no `missing_searches`.

`too_weak` is a disagreement too, and letting it through would mean accepting
a result the critic said was wrong. Stricter, and bounded by
`router_max_rounds` anyway.

### 7. `cross_criterion_check` catches a different inconsistency

**Plan** (01 §4) described "quotes citing the same chunk under opposite
readings — `met` in one, the related sub-requirement `missing` in another with
that chunk never retrieved."

That is not computable from the results alone: a `missing` sub-requirement has
no quotes by construction, so there is no chunk to compare. **Built**: a
criterion marking a sub-requirement `missing` while *another* criterion quotes
a verified passage that mentions that sub-requirement's distinctive terms.
Same intent — an inconsistency only the fan-in can see — and it reuses the
coverage check's vocabulary machinery. Still deterministic, still a note and
never an edit.

### 8. Two new database columns, and a migration mechanism

**Plan** (04 §1) assumed the reserved `evaluator_accepted/revised/fallback`
columns were enough. They were not: `unevaluated` is a third outcome the plan
itself introduced (§4), and the KPI page wants the critic's cost share.

`CREATE TABLE IF NOT EXISTS` cannot add a column to a database that already
exists, so `db.apply_schema` grew a guarded, idempotent `ALTER TABLE` list
(`_ADDED_COLUMNS`). Append-only by design: dropping or retyping a column is a
migration with a data question in it and would need more than a list. Existing
databases keep their rows; nothing has to be deleted and re-ingested.

The columns stay **nullable** rather than defaulting to 0, because a row
written before any of this must go on saying "nobody was asked" rather than
"nothing was accepted".

### 9. `analyze_criterion` kept its name

**Plan** (04 §1) had it "become the Analyzer's round-0 entry". It did, and it
kept the name, returning `AnalysisOutcome` instead of `ComplianceResult`. The
one production caller moved to `route_criterion` in the same commit, so no
alias existed for even one commit.

Timing and the `result` event moved out of it as planned — but in two hops
rather than one, landing briefly in `report.py` (commit `fd049b2`) before
reaching the Router (`3aeed91`), so that each commit left the tree green and
the event never went missing.

### 10. A `decision` SSE event the plan did not name

Step 7 named `evaluating` and `revising`. `decision` was added because a
subscriber that sees "evaluating" and then a result cannot tell whether the
critic agreed or the rounds ran out. It carries the verdict, the mode and the
reason codes — the same payload as the `router.decision` log line.

## Things worth saying about the result

* **`agent.py`'s module docstring used to open "This is the whole of the
  'Router Agent'".** Under this design that loop is the *Analyzer*. Left
  alone, the demo's own source would have argued against its architecture
  diagram. Fixed in `fd049b2`.
* **The isolation is tested three times, at three levels**: on the schema
  (`test_agent_protocol.py` — `EvaluationRequest` has no field for a
  conversation), on the built request (`test_router.py` — only cited passages),
  and on the wire (`test_evaluator.py` — no `tool_use` or `tool_result` string
  anywhere in the request body). It is the claim the whole design rests on, so
  it is the claim asserted most.
* **One test earns its keep more than the others**:
  `test_an_evaluator_that_cannot_answer_lowers_the_result_but_never_blocks_it`.
  Three unusable critic answers, and the analysis still ships — flagged
  `unevaluated`, capped, with the analyst's own numbers and no fabricated
  agreement.
* **Mean confidence in the report test moved 0.9 → 0.85.** 0.9 was the
  analyst's own estimate; 0.85 is what survives meeting a critic that *agrees*.
  Updated rather than loosened, per 04 §4.

## Next, in order

1. Step 7's UI half — the two progress states, the verdict badge, the findings
   drawer, the KPI tiles (accept rate, revise rate, verdict-changed-by-revision,
   evaluator cost share). Everything they need is already on the wire.
2. Confidence D1 — Wilson intervals on the KPI proportions. Cheapest honest
   upgrade in the whole plan; pure functions and a test.
3. Confidence B1 — reviewer overrides, which is what starts the label flow
   everything downstream is gated on.
