"""The Router: the agent that owns the conversation between the other two.

It builds the task from the criterion, invokes the Analyzer, packages the
result and *only the evidence that result cites* as JSON for the Evaluator,
receives structured findings, decides what happens next, and repeats the
calls when a revision is warranted. After the criteria fan in, it runs the
cross-criterion consistency pass.

It owns every decision about **process**. It makes no decision about
**content**: the retrieval routing is the Analyzer's, one logged tool choice
at a time, and the judgement of whether a quote carries its claim is the
Evaluator's. What sits here is the layer nothing owned before -- which
evidence the critic gets to see, when to stop, and what a second opinion may
cost.

**Why this one is not a model call.** Every input to `decide` is already a
structured judgement produced by a model or by a rule. A model re-reading
that JSON to choose `accept` over `revise` would add latency, cost and a new
failure mode while being un-unit-testable; the policy below is a table, and a
test pins it. The intelligence belongs at the ends. If a finding class ever
genuinely needs discretion -- "is this hedge material?" -- the discretion
belongs in the *Evaluator's* rubric, and the Router goes on reading verdicts.

**Why the Evaluator's view is the Router's job.** A critic handed the
Analyzer's conversation inherits the reasoning that made the error. The
Router is the component that guarantees it was blind, `EvaluationRequest` is
the shape that makes the guarantee structural, and the JSON on the
`router.decision` span is the proof for anyone who wants to check.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from ..compliance.criteria import Criterion
from ..compliance.schemas import (
    CitedPassage,
    ClaimedQuote,
    ComplianceResult,
    EvaluationRequest,
    EvaluatorFindings,
    RevisionRequest,
)
from ..config import Settings, get_settings
from ..embeddings.base import Embedder
from ..logger import get_logger, span
from .agent import OnEvent
from .analysis import (
    AnalysisOutcome,
    analyze_criterion,
    compute_confidence,
    revise,
    undetermined_count,
)
from .client import Usage, get_client
from .evaluator import (
    Evaluation,
    EvaluationFailed,
    content_terms,
    distinctive_terms,
    evaluate,
)

log = get_logger(__name__)

Verdict = Literal["accept", "revise", "fallback", "unevaluated"]
Mode = Literal["redraft", "research"]

#: Tool arguments that say *what* was searched for. `mode` and `top_k` say how.
QUERY_ARGS = ("query", "section", "label", "prefix")


@dataclass
class RouterDecision:
    """What the Router decided, and every reason it decided it.

    Logged whole on `router.decision`. The reason codes are what the KPI page
    counts; the instructions are what the Analyzer is told, and they name
    defects rather than answers.
    """

    verdict: Verdict
    round: int
    mode: Mode | None = None
    reasons: list[str] = field(default_factory=list)
    instructions: list[str] = field(default_factory=list)


# ==========================================================================
# What the Evaluator is allowed to see
# ==========================================================================


def build_evaluation_request(outcome: AnalysisOutcome, *, round: int) -> EvaluationRequest:
    """The result's claims, the passages its quotes cite, and nothing else.

    Not the whole ledger: a passage the draft never quoted is not evidence
    for a claim the draft never made, and handing it over invites the critic
    to write the assessment instead of checking it. Not the conversation, not
    the thinking, not the tool results -- `EvaluationRequest` has no field for
    any of them.

    The tool-call queries do go over, because the search-coverage check is a
    fact about the log and the Router is the only component that can see the
    log. Queries, not results: what was looked for, never what came back.
    """
    result = outcome.result
    criterion = outcome.criterion
    cited = {quote.evidence_id for quote in result.relevant_quotes}
    passages = [
        CitedPassage(
            evidence_id=entry.id,
            section_path=entry.chunk.breadcrumb or entry.chunk.section,
            page=entry.chunk.page_display,
            text=entry.chunk.text_for_model(),
        )
        for entry in outcome.evidence
        if entry.id in cited
    ]
    queries = [
        value
        for call in outcome.run.tool_calls
        for name, value in call.args.items()
        if name in QUERY_ARGS and isinstance(value, str) and value.strip()
    ]
    return EvaluationRequest(
        criterion_id=criterion.id,
        requirement=criterion.requirement,
        question=criterion.question,
        compliance_state=result.compliance_state,
        sub_requirements=list(result.sub_requirements),
        quotes=[
            ClaimedQuote(index=i, text=quote.text, evidence_id=quote.evidence_id)
            for i, quote in enumerate(result.relevant_quotes)
        ],
        passages=passages,
        rationale=result.rationale,
        searched_queries=queries,
        round=round,
    )


# ==========================================================================
# The decision policy
# ==========================================================================


def decide(findings: EvaluatorFindings, round: int, settings: Settings) -> RouterDecision:
    """`accept`, `revise(mode)` or `fallback`. Deterministic, and a table.

    `research` outranks `redraft` when both apply: a redraft over evidence
    that was never retrieved can only relabel, not learn.
    """
    reasons: list[str] = []
    instructions: list[str] = []

    for judged in findings.disputed_quotes:
        reasons.append(f"quote_{judged.support}")
        instructions.append(
            f"relevant_quotes[{judged.quote_index}] was cited for sub-requirement "
            f"{judged.sub_requirement_id}, and a reviewer reading only that passage "
            f"found it {judged.support}: {judged.note}"
        )
    for agreement in findings.disputed_statuses:
        reasons.append(f"status_{agreement.agreement}")
        instructions.append(
            f"the status given for sub-requirement {agreement.sub_requirement_id} is "
            f"{agreement.agreement.replace('_', ' ')} for the language quoted: {agreement.note}"
        )
    if findings.state_agreement == "disagree":
        reasons.append("state_disagreement")
        instructions.append(
            "compliance_state does not follow from the evidence as a second reader sees it; "
            "re-derive it from the sub-requirement statuses you end up with"
        )
    for sub_id in findings.missing_searches:
        reasons.append("unsearched_requirement")
        instructions.append(
            f"sub-requirement {sub_id} was marked missing, but no search in this run went "
            f"looking for it; search for it before concluding it is absent"
        )

    if not reasons:
        return RouterDecision(verdict="accept", round=round)
    if round >= settings.router_max_rounds:
        # Rounds exhausted with findings still open. The result ships anyway,
        # flagged and capped -- a stuck loop never blocks the demo, the same
        # principle the structural fix rounds already follow.
        return RouterDecision(verdict="fallback", round=round, reasons=reasons)
    mode: Mode = "research" if findings.missing_searches else "redraft"
    return RouterDecision(
        verdict="revise", round=round, mode=mode, reasons=reasons, instructions=instructions
    )


# ==========================================================================
# The loop
# ==========================================================================


def route_criterion(
    criterion: Criterion,
    conn: sqlite3.Connection,
    embedder: Embedder | None = None,
    settings: Settings | None = None,
    *,
    document_id: int,
    client: Any = None,
    on_event: OnEvent | None = None,
) -> ComplianceResult:
    """One criterion, end to end: analyse, evaluate, decide, maybe revise.

    This is what the harness calls. It replaces the bare `analyze_criterion`
    call, and it is the frame that owns the criterion's whole timeline -- so
    the latency and the `result` event are emitted here, where "how long did
    criterion 3 take" has a truthful answer.
    """
    settings = settings or get_settings()
    client = client or get_client(settings)
    emit = on_event or (lambda event: None)

    with span("router.criterion", log, criterion=criterion.id, document_id=document_id) as bag:
        started = time.perf_counter()
        outcome = analyze_criterion(
            criterion, conn, embedder, settings,
            document_id=document_id, client=client, on_event=on_event,
        )
        evaluation: Evaluation | None = None
        evaluator_cost = 0.0
        evaluator_usage = Usage()
        candidates: list[tuple[AnalysisOutcome, Evaluation]] = []
        decision = RouterDecision(verdict="unevaluated", round=0)

        for round_no in range(settings.router_max_rounds + 1):
            emit({"type": "evaluating", "surface": "analysis", "criterion": criterion.id,
                  "round": round_no})
            try:
                evaluation = evaluate(
                    build_evaluation_request(outcome, round=round_no), criterion,
                    settings=settings, client=client,
                )
            except EvaluationFailed as exc:
                # Degradation, not failure. The Evaluator may lower what ships;
                # it may not be the reason nothing ships. The failed attempts
                # are still booked: the money was spent, answer or not.
                evaluator_cost += exc.cost_usd
                evaluator_usage += exc.usage
                log.warning(
                    "router.unevaluated",
                    extra={"criterion": criterion.id, "round": round_no, "error": str(exc)},
                )
                evaluation = None
                decision = RouterDecision(
                    verdict="unevaluated", round=round_no, reasons=["evaluator_failed"]
                )
                break
            evaluator_cost += evaluation.cost_usd
            evaluator_usage += evaluation.usage
            candidates.append((outcome, evaluation))
            decision = decide(evaluation.findings, round_no, settings)
            log.info(
                "router.decision",
                extra={"criterion": criterion.id, "round": round_no, "verdict": decision.verdict,
                       "mode": decision.mode, "reasons": decision.reasons},
            )
            emit({"type": "decision", "surface": "analysis", "criterion": criterion.id,
                  "round": round_no, "verdict": decision.verdict, "mode": decision.mode,
                  "reasons": decision.reasons})
            if decision.verdict != "revise":
                break
            outcome = revise(
                outcome,
                RevisionRequest(
                    mode=decision.mode or "redraft",
                    round=round_no + 1,
                    instructions=decision.instructions,
                    reason_codes=decision.reasons,
                ),
                settings=settings, client=client, on_event=on_event,
            )

        rounds_spent = outcome.rounds
        if decision.verdict == "fallback" and candidates:
            # Rounds are exhausted with findings still open, so *some* round
            # ships flagged -- and it should be the best one, not the last one.
            # A failed revision can come back worse than the draft it revised
            # (a live run's redraft lost six sub-requirements to a degenerate
            # draft); returning it just because it is newest would replace a
            # mediocre result with a broken one.
            outcome, evaluation = best_round(candidates)
            if outcome.rounds != rounds_spent:
                # An earlier round ships, but the whole loop was paid for. The
                # run object is shared across rounds and holds every leg's
                # spend; the earlier result's snapshot of it is stale.
                outcome.result.usage = outcome.run.usage.as_dict()
                outcome.result.cost_usd = outcome.run.cost_usd
                outcome.result.tool_calls = len(outcome.run.tool_calls)
                log.info(
                    "router.shipped_earlier_round",
                    extra={"criterion": criterion.id, "shipped_round": outcome.rounds,
                           "rounds_spent": rounds_spent},
                )
        result = finalize(
            outcome, evaluation, decision,
            evaluator_cost_usd=evaluator_cost, evaluator_usage=evaluator_usage,
            rounds=rounds_spent,
        )
        result.latency_s = round(time.perf_counter() - started, 3)
        emit({"type": "result", "surface": "analysis", "criterion": criterion.id,
              "state": result.compliance_state, "confidence": result.confidence,
              "needs_review": result.needs_review, "latency_s": result.latency_s,
              "verdict": result.verdict, "rounds": result.rounds})
        bag.update(
            verdict=result.verdict,
            rounds=result.rounds,
            state=result.compliance_state,
            confidence=result.confidence,
            needs_review=result.needs_review,
            latency_s=result.latency_s,
            evaluator_cost_usd=round(evaluator_cost, 6),
            cost_usd=result.cost_usd,
        )
    return result


def best_round(
    candidates: Sequence[tuple[AnalysisOutcome, Evaluation]],
) -> tuple[AnalysisOutcome, Evaluation]:
    """The round to ship when rounds run out with findings still open.

    Fewest unresolved structural errors first -- a round that lost quotes or
    whole sub-requirements to surviving errors is broken in a way no critic
    note offsets. Then the round the critic rated higher; then the later
    round, which at least saw the instructions. Deterministic on purpose:
    which round ships on a fallback is a process decision, and process
    decisions in this module are tables.
    """
    return min(
        candidates,
        key=lambda pair: (
            len(pair[0].result.unresolved_errors),
            -pair[1].findings.critic_confidence,
            -pair[0].rounds,
        ),
    )


def finalize(
    outcome: AnalysisOutcome,
    evaluation: Evaluation | None,
    decision: RouterDecision,
    *,
    evaluator_cost_usd: float = 0.0,
    evaluator_usage: Usage | None = None,
    rounds: int | None = None,
) -> ComplianceResult:
    """Compose the confidence, attach the findings, record how it ended.

    The findings land on the result whatever was decided -- including on
    `accept` -- so the UI can show *why* the confidence is what it is and the
    KPI page reads agreement rates off stored results rather than off logs.

    Anything but `accept` sets `needs_review`, which caps the confidence at
    0.5 through the same term a structural failure uses. `verdict` is what
    distinguishes the three ways that can happen, so the UI can say which.

    `rounds` is what the *loop* spent, not which round's draft ships: on a
    best-of-rounds fallback those differ, and the KPI page's revise rate
    counts the loop. The evaluator's tokens fold into `result.usage` the same
    way its cost folds into `cost_usd` -- the totals answer for every request
    the criterion made, whichever agent made it.
    """
    result = outcome.result
    verdict: Verdict = "accept" if decision.verdict == "accept" else decision.verdict
    if verdict == "revise":  # pragma: no cover - the loop never exits on `revise`
        verdict = "fallback"
    findings = evaluation.findings if evaluation is not None else None

    needs_review = result.needs_review or verdict != "accept"
    claimed = len(result.relevant_quotes)
    # Each quote is scored against the status it was cited for: partial support
    # for a `partial` claim is agreement, not a half-failure.
    statuses = {sub.id: sub.status for sub in result.sub_requirements}
    confidence, components = compute_confidence(
        result.raw_confidence,
        verified=sum(1 for q in result.relevant_quotes if q.verified),
        claimed=claimed,
        not_determined=undetermined_count(result.sub_requirements, outcome.criterion),
        total=len(outcome.criterion.sub_requirements),
        needs_review=needs_review,
        ended_by=result.ended_by,
        critic=findings.critic_confidence if findings else None,
        support_ratio=findings.support_ratio(statuses) if findings else None,
        state_agreed=findings.state_agreement == "agree" if findings else True,
    )
    result.confidence = confidence
    result.confidence_components = components
    result.needs_review = needs_review
    result.verdict = verdict
    result.rounds = outcome.rounds if rounds is None else rounds
    result.evaluator_findings = findings
    result.evaluator_cost_usd = round(evaluator_cost_usd, 6)
    result.cost_usd = round(result.cost_usd + evaluator_cost_usd, 6)
    if evaluator_usage is not None:
        for key, value in evaluator_usage.as_dict().items():
            result.usage[key] = result.usage.get(key, 0) + value
    return result


# ==========================================================================
# After fan-in
# ==========================================================================


def cross_criterion_check(results: Sequence[ComplianceResult]) -> list[str]:
    """Inconsistencies only visible across criteria. Deterministic, no model.

    The one worth catching: criterion A says it found no language for a
    sub-requirement, while criterion B quotes a passage that talks about
    exactly that. Each run is right about what *it* retrieved -- the five run
    in parallel and never see each other -- so neither the Analyzer nor the
    Evaluator can notice. Only the fan-in can, which makes it a Router duty:
    a process observation across runs, not a content judgement within one.

    A note, never an edit. The finding is that two runs disagree, and which
    of them is wrong is not something this function can know.
    """
    quotes = [
        (result.criterion_id, quote)
        for result in results
        for quote in result.relevant_quotes
        if quote.verified
    ]
    notes: list[str] = []
    for result in results:
        for sub in result.sub_requirements:
            if sub.status != "missing":
                continue
            terms = distinctive_terms(
                result.compliance_requirement, result.sub_requirements, sub.id
            )
            for other_id, quote in quotes:
                if other_id == result.criterion_id:
                    continue
                shared = terms & content_terms(quote.text)
                if not shared:
                    continue
                where = quote.section_ref or quote.evidence_id
                page = f", p.{quote.page_display}" if quote.page_display else ""
                notes.append(
                    f"{result.criterion_id} marked {sub.id!r} missing, but {other_id} quotes "
                    f"{where}{page}, which mentions {', '.join(sorted(shared))}"
                )
                break  # one note per gap: the first contradiction is the finding
    return notes


__all__ = [
    "RouterDecision",
    "best_round",
    "build_evaluation_request",
    "cross_criterion_check",
    "decide",
    "finalize",
    "route_criterion",
]
