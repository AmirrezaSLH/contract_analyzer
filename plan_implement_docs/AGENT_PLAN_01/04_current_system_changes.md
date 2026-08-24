# How the current system changes

**Status: done**, except step 7's UI half. What actually landed, including the two database columns and the migration mechanism this file did not anticipate, is in [06_build_report.md](06_build_report.md).

What lands where, what moves, what must not break, and the behaviour the
demo will feel. Companion to the per-agent designs; file paths are the
repo's today.

## 1. File-by-file

| File | Change | Kind |
|---|---|---|
| `generation/router.py` | **New.** `route_criterion`, `decide`, `EvaluationRequest.build`, `finalize` (confidence composition), `cross_criterion_check` | add |
| `generation/evaluator.py` | **New.** E1 pre-checks, critic call, findings parse + retry | add |
| `generation/agent.py` | Loop body extracted so a run can resume from an existing `(messages, tools)` pair (the `research` revision). `run_agent`'s signature and behaviour for fresh runs unchanged | refactor, no behaviour change |
| `generation/analysis.py` | `analyze_criterion` becomes the Analyzer's round-0 entry, returning `AnalysisOutcome` (result + ledger + conversation + tools) instead of discarding them; new `revise(outcome, request)` with `redraft`/`research` modes; `compute_confidence` gains the critic terms (old signature kept for old-report tooling); the per-criterion span/latency/`result`-event emission moves up into the Router, which now owns the whole criterion timeline | modify |
| `compliance/schemas.py` | **New models:** `QuoteSupport`, `StatusAgreement`, `EvaluatorFindings`, `EvaluationRequest`, `RevisionRequest`. `ComplianceResult` gains `verdict: Literal["accept","fallback","unevaluated"] = "unevaluated"`, `rounds: int = 0`, `evaluator_findings: EvaluatorFindings \| None = None`; `confidence_components` gains `critic`/`agreement` keys | extend, defaults keep old JSON parsing |
| `compliance/validate.py` | Unchanged. Remains the Analyzer's structural gate; hedge lexicon and search coverage live in `evaluator.py`, not here — validate answers "is it well-formed", the Evaluator answers "is it right" | none |
| `generation/prompts.json` / `prompts.py` | New keys `evaluator.system`, `evaluator.user`, `analysis.revise`; all three added to `REQUIRED_KEYS` (a stale prompts file now fails fast at load, as designed) | extend |
| `config.py` + `settings.json` | New: `evaluator_model` (default = `analysis_model`), `evaluator_effort` (`medium`), `evaluator_max_tokens` (2000), `router_max_rounds` (1), `research_extra_tool_calls` (3) | extend |
| `report.py` | Calls `router.route_criterion` instead of `analyze_criterion`; after fan-in calls `cross_criterion_check` to fill the already-existing `cross_criterion_notes`; `AnalysisTotals` gains `revised` and `evaluator_cost_usd` | modify |
| `api/` (sse, routes, schemas) | New SSE event types `evaluating`, `revising` pass through the existing generic event plumbing; `result` event gains `verdict`/`rounds`. Response schemas widen with the new result fields | extend |
| `ui/` | Progress view: two new transient states per criterion ("evaluating", "revising"); result card: verdict badge + findings drawer; KPI page: accept-rate / revise-rate / verdict-changed tiles and evaluator cost share | extend |
| `tests/` | New `test_router.py`, `test_evaluator.py`, revise tests; existing analysis tests need scripted transports extended with the critic's structured reply (see §4) | extend |
| `docs/` | New `docs/agents.md` (three-agent diagram + protocol); `docs/generation.md` and README updated; slide deck gains the architecture slide | extend |

## 2. Behaviour changes the demo will feel

* **Latency.** Each criterion gains one critic call (~2–5 s at medium
  effort) and, when the Router revises, one finisher round or a short tool
  leg (~5–15 s). Criteria still run in parallel, so wall clock for a
  five-criterion run moves from ~60 s toward ~75–90 s with occasional
  revisions. Acceptable; the progress view now *shows* the extra phases
  instead of a longer silence.
* **Cost.** +5 critic calls per contract (~2k tokens each) ≈ one extra
  analysis run; each revision adds roughly one finisher call. `cost_usd`
  on results and totals absorbs it, so the KPI cost tiles stay truthful
  with no changes.
* **Concurrency envelope.** `api_workers × analysis_workers` remains the
  in-flight ceiling; the critic call happens inside the criterion's worker
  slot (serial with its analysis), so the ceiling does not widen — a run
  just holds its slot slightly longer.
* **Confidence values shift.** `min(raw, critic)` and the support-based
  quote term mean scores will generally drop, especially where the critic
  disagrees. This is the point — but historical comparisons on the KPI
  page cross a regime boundary; the metrics store keeps flowing, and the
  dashboard should annotate the change rather than pretend continuity.
* **`needs_review` changes meaning slightly.** Today: structural failure
  or cap. After: also fallback/unevaluated verdicts. The UI copy should
  say *why* (the verdict field distinguishes them).

## 3. Compatibility invariants (the things that must not break)

* **Old persisted reports parse.** All new `ComplianceResult` fields have
  defaults; `AnalysisReport` is unchanged except that
  `cross_criterion_notes` starts being non-empty — the field has existed
  since step 10 precisely so this moment would not change the wire format.
* **`GET /analyses/{id}` shape widens, never narrows.** Clients ignoring
  the new fields keep working (the React app reads what it knows).
* **CLI = API.** `make analyze` goes through `report.py` and therefore
  through the Router automatically; the invariant that the HTTP layer has
  no logic the command line lacks is preserved without extra work.
* **One transport.** The critic call uses `generation/client.py` and
  `call_model`'s streaming path; no new HTTP stack, retries stay in one
  place.
* **Trace correlation.** All new spans use the existing `span()`/trace-id
  machinery; `copy_context().run` in `report.py` already carries the trace
  onto worker threads, and the Router runs inside the worker, so nothing
  new is needed for log reconstruction.
* **No-key behaviour.** `AnswerUnavailable` is still raised before any
  request; the Evaluator adds no earlier network touch.

## 4. Test-suite impact

The scripted-transport tests currently serve: N tool turns → one
structured draft (→ optional fix rounds). Every analysis-path test now
needs **one more scripted structured reply** (the critic's findings) per
criterion, and `settings` overrides gain `router_max_rounds`. Cheapest
path: a fixture helper `clean_findings(draft)` that generates an
all-agree `EvaluatorFindings` for the common case, so existing tests
change by one line each; disagreement cases are authored only in the new
router/evaluator tests. Tests that assert `confidence` numbers need
updating for the new formula — update the expected components dict rather
than loosening assertions.

## 5. Deliberately not changing

* **`report.py` stays the harness** — threads, SQLite connection-per-
  criterion, event serialisation, the analyses record. The Router is logic
  inside the worker, not a rearrangement of the process model.
* **Chat is untouched.** The chat surface keeps its own loop and budgets;
  evaluation is a compliance-analysis concern.
* **No framework, no queue.** Agent-to-agent messages are Pydantic-JSON
  across function calls; the day an out-of-process evaluator is wanted,
  `EvaluationRequest`/`EvaluatorFindings` already serialise.
* **`validate.py` and the structural fix rounds** stay exactly as they
  are — the Evaluator sits after them, never replaces them.
* **Retrieval knobs** (`analysis_max_tool_calls`, `max_evidence_tokens`)
  keep their meanings; `research` grants a delta, it does not reset.

## 6. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Critic over-flags → revise loops burn cost | `router_max_rounds=1`; KPI revise-rate tile watched during the demo prep; rubric tuned before raising rounds |
| Critic under-flags (rubber stamp) | verdict-changed-by-revision tile + stored findings make it measurable; effort/model are per-config levers |
| Confidence regime change confuses the dashboard | components stored per result; dashboard annotates the boundary date |
| Refactor of `analyze_criterion` breaks the SSE contract | the `result` event's existing fields are pinned by `tests` on the API side; extend, don't rename |
| Prompt-cache miss on revise rounds | revisions continue the same conversation with the same `tools → system` prefix (the finisher already established the pattern) |
