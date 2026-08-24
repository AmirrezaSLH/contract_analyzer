"""Surface 1: the compliance analysis. One agent run per criterion, then a
structured finisher that corrects its own structure, bounded.

The model searches through the tools; when it stops, the finisher asks for
a `ComplianceDraft` with `output_config.format` -- citations off, the tool
definitions kept but `tool_choice: none`, the ledger *not* re-sent, since
every passage is already in the conversation as a tool result. The draft is
validated in pure Python against the ledger and the schema's cross-field
rules; the errors go back as one user turn and
the model tries again, up to `structure_fix_rounds` times. Feedback names
what is malformed, never what the answer should be.

When errors survive every round, the result is still returned: every quote
that failed is dropped, `needs_review` is set and confidence is capped at
0.5. A bad quote never reaches the UI; a stuck loop never blocks the demo.

Truncation and refusal are retried once as plain retries -- there is no
structure to correct in an answer that did not arrive.

**Confidence -- first design, deliberately simple.** Three terms, each a
sentence: the model's own estimate, cut by the share of its quotes that were
not verbatim, cut by the share of the criterion it could not find language
for; capped at 0.5 when the result needs review or a cap ended the run;
clamped to [0.05, 0.95] because nothing here is certain either way. The
components are stored on the result so a later design can be fitted without
changing the schema.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from ..compliance.criteria import Criterion
from ..compliance.schemas import (
    ComplianceDraft,
    ComplianceResult,
    ResolvedQuote,
    RevisionRequest,
)
from ..compliance.validate import StructuralError, quote_in_chunk, validate_structure
from ..config import Settings, get_settings
from ..embeddings.base import Embedder
from ..logger import get_logger, span
from .agent import (
    AgentRun,
    AgentTask,
    OnEvent,
    call_model,
    content_params,
    resume_agent,
    run_agent,
    text_of,
)
from .client import Usage, get_client
from .prompts import get_prompts
from .tools import ContractTools, Evidence

log = get_logger(__name__)

#: The finisher wants a draft, not another search -- but the definitions stay.
NO_TOOLS: dict[str, str] = {"type": "none"}

#: Stop reasons that mean "no draft arrived", retried once as plain retries.
RETRY_STOPS = frozenset({"max_tokens", "refusal"})

CONFIDENCE_FLOOR, CONFIDENCE_CEILING = 0.05, 0.95
REVIEW_CAP = 0.5

#: What the score is multiplied by when the critic reads the evidence as a
#: different compliance state. Not zero: two careful readers disagreeing means
#: the answer is uncertain, not that the analyst's is wrong.
STATE_DISAGREEMENT = 0.6


class AnalysisFailed(RuntimeError):
    """The finisher could not obtain a draft at all (truncated or refused twice)."""


@dataclass
class AnalysisOutcome:
    """One Analyzer round, kept rather than discarded.

    This function used to return the result and throw away everything that
    produced it. The Router needs the rest: the **ledger**, to slice the
    cited passages out of for the Evaluator's request; the **conversation**,
    to continue on a redraft round; the **live tools**, to search again on a
    research round without resetting the budget or re-burning the index.
    Nothing here is recomputed -- it is the run, kept.

    `result.confidence` at this point is the Analyzer's own estimate. The
    Router recomposes it once the critic has spoken (`router.finalize`); a
    result that never reaches a Router keeps the analyst's number, which is
    what it always was.
    """

    criterion: Criterion
    result: ComplianceResult
    run: AgentRun
    tools: ContractTools
    #: The run's system prompt, kept so a revision's request has the identical
    #: `tools -> system` prefix and hits the prompt cache.
    system: str
    #: Revision rounds spent. 0 is the first draft.
    rounds: int = 0

    @property
    def evidence(self) -> Evidence:
        return self.run.evidence

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self.run.messages


def analyze_criterion(
    criterion: Criterion,
    conn: sqlite3.Connection,
    embedder: Embedder | None = None,
    settings: Settings | None = None,
    *,
    document_id: int,
    client: Any = None,
    on_event: OnEvent | None = None,
) -> AnalysisOutcome:
    """The Analyzer, round 0: assess one contract against one criterion.

    Raises `AnswerUnavailable` before any request if there is no key, and
    `AnalysisFailed` when no draft ever arrived.

    Timing and the `result` event belong to whoever owns the whole criterion
    timeline -- with a Router in front, that is the Router, because a
    criterion now takes an evaluation and possibly a revision after this
    returns. This function times and announces nothing.
    """
    settings = settings or get_settings()
    client = client or get_client(settings)
    prompts = get_prompts(settings)
    tools = ContractTools(conn, document_id=document_id, embedder=embedder, settings=settings)
    system = (
        prompts.get("agent.system")
        + "\n\n"
        + prompts.format(
            "analysis.system",
            requirement=criterion.requirement,
            question=criterion.question,
            sub_requirements=criterion.sub_requirements_text(),
        )
    )
    task = AgentTask(
        surface="analysis",
        system=system,
        messages=[{"role": "user", "content": prompts.get("analysis.user")}],
        effort=settings.analysis_effort,
        max_tool_calls=settings.analysis_max_tool_calls,
    )

    def finisher(run: AgentRun) -> ComplianceResult:
        return finish_analysis(
            run, criterion=criterion, settings=settings, client=client, system=system,
            tools=tools.definitions(), on_event=on_event or (lambda event: None),
        )

    with span("analysis.criterion", log, criterion=criterion.id, document_id=document_id) as bag:
        run = run_agent(
            task, tools=tools, finisher=finisher, settings=settings, client=client,
            model=settings.analysis_model, on_event=on_event,
        )
        result: ComplianceResult = run.result
        bag.update(
            state=result.compliance_state,
            confidence=result.confidence,
            needs_review=result.needs_review,
            structure_rounds=result.structure_rounds,
            ended_by=run.ended_by,
            cost_usd=result.cost_usd,
        )
    return AnalysisOutcome(
        criterion=criterion, result=result, run=run, tools=tools, system=system
    )


def finish_analysis(
    run: AgentRun,
    *,
    criterion: Criterion,
    settings: Settings,
    client: Any,
    system: str,
    tools: list[dict[str, Any]],
    on_event: Callable[[dict[str, Any]], None],
) -> ComplianceResult:
    """The structured finisher: draft, validate, correct, resolve.

    `tools` are the loop's definitions, sent again with `tool_choice: none`,
    so the finisher's prefix matches the loop's for caching and the tool
    blocks in the conversation always have their definitions alongside.
    """
    prompts = get_prompts(settings)
    return _finish(
        run, prompts.get("analysis.finish"), criterion=criterion, settings=settings,
        client=client, system=system, tools=tools, on_event=on_event,
    )


def _finish(
    run: AgentRun,
    opening: str,
    *,
    criterion: Criterion,
    settings: Settings,
    client: Any,
    system: str,
    tools: list[dict[str, Any]],
    on_event: Callable[[dict[str, Any]], None],
) -> ComplianceResult:
    """Draft, validate, correct, resolve -- from `opening` as the user turn.

    Two callers, one machinery. `finish_analysis` opens with "you have
    finished searching, produce the assessment"; a revision opens with the
    Router's findings. Both then correct *structure* against `validate.py`
    for up to `structure_fix_rounds`, because a revision's draft can be as
    malformed as a first one.

    The conversation is written back to `run.messages` including the draft
    that ended it. That is what makes a revision cheap and honest: the next
    round continues the real conversation -- same prefix, same cache, same
    evidence -- instead of a reconstruction of it.
    """
    prompts = get_prompts(settings)
    messages = list(run.messages) + [{"role": "user", "content": opening}]
    rounds = 0
    while True:
        run.turns += 1
        message = _draft_call(run, client, settings, system, messages, tools)
        draft, errors = _parse(message, run.evidence, criterion)
        if errors:
            log.info(
                "analysis.structure_errors",
                extra={"criterion": criterion.id, "round": rounds, "count": len(errors),
                       "codes": sorted({e.code for e in errors})},
            )
            on_event({"type": "structure_errors", "surface": "analysis", "round": rounds,
                      "errors": [str(e) for e in errors]})
        if not errors or rounds >= settings.structure_fix_rounds:
            break
        rounds += 1
        messages.append({"role": "assistant", "content": content_params(message)})
        messages.append(
            {
                "role": "user",
                "content": prompts.format(
                    "analysis.fix_structure", errors="\n".join(f"- {e}" for e in errors)
                ),
            }
        )
    if draft is None:
        raise AnalysisFailed(
            f"the model never produced a parseable draft for {criterion.id}: "
            + "; ".join(str(e) for e in errors)
        )
    run.messages = messages + [{"role": "assistant", "content": content_params(message)}]
    # The `result` event is *not* emitted here. The Router emits it, because
    # only the Router knows how long the criterion took and how it ended -- and
    # a progress row told the verdict now and the latency later is two events
    # for one fact.
    return build_result(draft, errors, run, criterion=criterion, structure_rounds=rounds)


def revise(
    outcome: AnalysisOutcome,
    revision: RevisionRequest,
    *,
    settings: Settings | None = None,
    client: Any = None,
    on_event: OnEvent | None = None,
) -> AnalysisOutcome:
    """Another round on the same conversation, in the mode the Router chose.

    * **redraft** -- the findings go in as one user turn and the finisher runs
      again with `tool_choice: none`. One structured call. This is the right
      move when the evidence is all there and the reading of it is what was
      disputed.
    * **research** -- the findings go in *with tools enabled* and the loop is
      re-entered with `research_extra_tool_calls` granted on top of what was
      already spent, then the finisher runs. This is the right move when a
      sub-requirement was called missing without ever being searched for: a
      redraft over evidence that was never retrieved can only relabel, not
      learn.

    The ledger, the dedupe table and the token budget carry over in both
    modes, so a repeated query is answered from the ledger at zero retrieval
    cost and a revision cannot re-burn the index.
    """
    settings = settings or get_settings()
    client = client or get_client(settings)
    prompts = get_prompts(settings)
    emit = on_event or (lambda event: None)
    criterion, run, tools = outcome.criterion, outcome.run, outcome.tools
    feedback = prompts.format("analysis.revise", findings=revision.text())
    definitions = tools.definitions()
    spent = len(tools.calls)

    with span(
        "analysis.revise", log, criterion=criterion.id, mode=revision.mode,
        round=revision.round,
    ) as bag:
        emit({
            "type": "revising", "surface": "analysis", "mode": revision.mode,
            "round": revision.round, "reasons": revision.reason_codes,
        })
        if revision.mode == "research":
            granted = settings.research_extra_tool_calls
            run = resume_agent(
                run,
                AgentTask(
                    surface="analysis",
                    system=outcome.system,
                    messages=[{"role": "user", "content": feedback}],
                    effort=settings.analysis_effort,
                    # Absolute, counted against calls already made: a delta,
                    # never a fresh allowance.
                    max_tool_calls=spent + granted,
                ),
                tools=tools,
                settings=settings,
                client=client,
                on_event=on_event,
            )
            result = finish_analysis(
                run, criterion=criterion, settings=settings, client=client,
                system=outcome.system, tools=definitions, on_event=emit,
            )
        else:
            result = _finish(
                run, feedback, criterion=criterion, settings=settings, client=client,
                system=outcome.system, tools=definitions, on_event=emit,
            )
        bag.update(
            extra_tool_calls=len(tools.calls) - spent,
            state=result.compliance_state,
            needs_review=result.needs_review,
            structure_rounds=result.structure_rounds,
            ended_by=run.ended_by,
        )
    return AnalysisOutcome(
        criterion=criterion, result=result, run=run, tools=tools,
        system=outcome.system, rounds=revision.round,
    )


def _draft_call(
    run: AgentRun, client: Any, settings: Settings, system: str, messages, tools
) -> Any:
    """One structured call, retried once if the answer was truncated or refused."""
    for attempt in range(2):
        message = call_model(
            client,
            model=run.model,
            max_tokens=settings.answer_max_tokens,
            system=system,
            messages=messages,
            effort=run.effort,
            surface="analysis",
            turn=run.turns,
            tools=tools,
            tool_choice=NO_TOOLS,
            format=ComplianceDraft.output_format(),
        )
        run.usage += Usage.from_message(message)
        if message.stop_reason not in RETRY_STOPS:
            return message
        log.warning(
            "analysis.draft_retry",
            extra={"stop_reason": message.stop_reason, "attempt": attempt + 1},
        )
    raise AnalysisFailed(f"no draft: the model stopped with {message.stop_reason!r} twice")


def _parse(
    message: Any, evidence: Evidence, criterion: Criterion
) -> tuple[ComplianceDraft | None, list[StructuralError]]:
    try:
        draft = ComplianceDraft.model_validate_json(text_of(message))
    except ValidationError as exc:
        return None, [StructuralError("$", "invalid", f"not a valid draft: {exc.errors()[0]}")]
    return draft, validate_structure(draft, evidence, criterion)


def build_result(
    draft: ComplianceDraft,
    errors: list[StructuralError],
    run: AgentRun,
    *,
    criterion: Criterion,
    structure_rounds: int,
) -> ComplianceResult:
    """Resolve quotes, drop the ones that failed, compute confidence."""
    needs_review = bool(errors)
    failed_quotes = {
        int(e.path[len("relevant_quotes["):].split("]")[0])
        for e in errors
        if e.path.startswith("relevant_quotes[")
    }
    kept: list[ResolvedQuote] = []
    index_map: dict[int, int] = {}
    for i, quote in enumerate(draft.relevant_quotes):
        if i in failed_quotes:
            continue
        entry = run.evidence.get(quote.evidence_id) if quote.evidence_id in run.evidence else None
        chunk = entry.chunk if entry else None
        index_map[i] = len(kept)
        kept.append(
            ResolvedQuote(
                text=quote.text,
                evidence_id=quote.evidence_id,
                section_ref=(chunk.section_path[-1] if chunk and chunk.section_path
                             else chunk.section if chunk else ""),
                page_display=chunk.page_display if chunk else "",
                chunk_id=chunk.chunk_id if chunk else None,
                verified=bool(chunk)
                and quote_in_chunk(quote.text, chunk.text_for_model(), chunk.content),
            )
        )
    sub_requirements = [
        sub.model_copy(
            update={"quote_indexes": [index_map[q] for q in sub.quote_indexes if q in index_map]}
        )
        for sub in draft.sub_requirements
    ]
    claimed = len(draft.relevant_quotes)
    verified = sum(1 for q in kept if q.verified)
    not_determined = sum(1 for s in sub_requirements if s.status == "not_determined")
    confidence, components = compute_confidence(
        draft.raw_confidence,
        verified=verified,
        claimed=claimed,
        not_determined=not_determined,
        total=len(criterion.sub_requirements),
        needs_review=needs_review,
        ended_by=run.ended_by,
    )
    return ComplianceResult(
        criterion_id=criterion.id,
        compliance_requirement=criterion.requirement,
        compliance_question=criterion.question,
        compliance_state=draft.compliance_state,
        sub_requirements=sub_requirements,
        relevant_quotes=kept,
        rationale=draft.rationale,
        raw_confidence=draft.raw_confidence,
        confidence=confidence,
        confidence_components=components,
        needs_review=needs_review,
        unresolved_errors=[str(e) for e in errors],
        structure_rounds=structure_rounds,
        ended_by=run.ended_by,
        tool_calls=len(run.tool_calls),
        usage=run.usage.as_dict(),
        cost_usd=run.cost_usd,
        model=run.model,
    )


def compute_confidence(
    raw: float,
    *,
    verified: int,
    claimed: int,
    not_determined: int,
    total: int,
    needs_review: bool,
    ended_by: str,
    critic: float | None = None,
    support_ratio: float | None = None,
    state_agreed: bool = True,
) -> tuple[float, dict[str, float]]:
    """A heuristic score, each term a sentence, every term stored.

        confidence = min(raw, critic)                 two estimates, take the pessimist
                   x quote_term                       did the evidence carry the claim
                   x (1 - not_determined / total)     how much of the criterion was settled
                   x (1.0 if the critic agrees on the state else 0.6)

    capped at 0.5 when the result needs review or a counter ended the run,
    clamped to [0.05, 0.95] because nothing here is certain either way.

    **This is not a calibrated probability and the UI must not call it one.**
    Calibration is a property measured over labelled results; until those
    labels exist no formula is calibrated, whatever it looks like. What this
    function does instead is keep every term separately in
    `confidence_components`, so a later phase can fit the *combination*
    against real labels without changing the schema or re-running anything.
    See `plan_implement_docs/AGENT_PLAN_01/05_confidence_plan.md`.

    The critic's terms are optional so the old three-term call still works:
    a result with no critic keeps the analyst's estimate, which is what it
    always was, and its components say so by omitting the critic keys.

    `quote_term` is the critic's *support* ratio when there is one, and the
    verbatim ratio otherwise. Verbatim-ness was always a proxy for support --
    it is what could be checked without a reader. Now that a reader judges
    support directly, the proxy steps aside; it stays a hard gate upstream in
    `validate.py`, where a non-verbatim quote is dropped before it can be
    judged at all.
    """
    raw = min(1.0, max(0.0, raw))
    verbatim_ratio = verified / claimed if claimed else 1.0
    quote_term = verbatim_ratio if support_ratio is None else support_ratio
    coverage = 1.0 - not_determined / total if total else 1.0
    estimate = raw if critic is None else min(raw, min(1.0, max(0.0, critic)))
    agreement = 1.0 if state_agreed else STATE_DISAGREEMENT
    value = estimate * quote_term * coverage * agreement
    capped = needs_review or ended_by == "cap"
    if capped:
        value = min(value, REVIEW_CAP)
    value = min(CONFIDENCE_CEILING, max(CONFIDENCE_FLOOR, value))
    components = {
        "raw": raw,
        "quote_term": round(quote_term, 3),
        "coverage": round(coverage, 3),
        "cap": REVIEW_CAP if capped else 1.0,
    }
    if critic is not None:
        # Present only when a critic actually spoke, so the components can
        # never be read as "the critic agreed" about a run nobody criticised.
        components["critic"] = round(min(1.0, max(0.0, critic)), 3)
        components["agreement"] = agreement
    return round(value, 3), components


__all__ = [
    "AnalysisFailed",
    "AnalysisOutcome",
    "analyze_criterion",
    "build_result",
    "compute_confidence",
    "finish_analysis",
    "revise",
]
