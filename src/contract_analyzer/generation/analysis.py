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
from typing import Any

from pydantic import ValidationError

from ..compliance.criteria import Criterion
from ..compliance.schemas import ComplianceDraft, ComplianceResult, ResolvedQuote
from ..compliance.validate import StructuralError, quote_in_chunk, validate_structure
from ..config import Settings, get_settings
from ..embeddings.base import Embedder
from ..logger import get_logger, span
from .agent import AgentRun, AgentTask, OnEvent, call_model, content_params, run_agent, text_of
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


class AnalysisFailed(RuntimeError):
    """The finisher could not obtain a draft at all (truncated or refused twice)."""


def analyze_criterion(
    criterion: Criterion,
    conn: sqlite3.Connection,
    embedder: Embedder | None = None,
    settings: Settings | None = None,
    *,
    document_id: int,
    client: Any = None,
    on_event: OnEvent | None = None,
) -> ComplianceResult:
    """Assess one contract against one criterion. Raises `AnswerUnavailable`
    before any request if there is no key."""
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
            on_event=on_event,
        )
        result: ComplianceResult = run.result
        bag.update(
            state=result.compliance_state,
            confidence=result.confidence,
            needs_review=result.needs_review,
            structure_rounds=result.structure_rounds,
            cost_usd=result.cost_usd,
        )
    return result


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
    messages = list(run.messages) + [{"role": "user", "content": prompts.get("analysis.finish")}]
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
    result = build_result(draft, errors, run, criterion=criterion, structure_rounds=rounds)
    on_event({"type": "result", "surface": "analysis", "criterion": criterion.id,
              "state": result.compliance_state, "confidence": result.confidence})
    return result


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
) -> tuple[float, dict[str, float]]:
    """confidence = raw x (verified/claimed) x (1 - not_determined/total),
    capped at 0.5 on `needs_review` or a capped run, clamped to [0.05, 0.95]."""
    raw = min(1.0, max(0.0, raw))
    quote_term = verified / claimed if claimed else 1.0
    coverage = 1.0 - not_determined / total if total else 1.0
    value = raw * quote_term * coverage
    capped = needs_review or ended_by == "cap"
    if capped:
        value = min(value, REVIEW_CAP)
    value = min(CONFIDENCE_CEILING, max(CONFIDENCE_FLOOR, value))
    return round(value, 3), {
        "raw": raw,
        "quote_term": round(quote_term, 3),
        "coverage": round(coverage, 3),
        "cap": REVIEW_CAP if capped else 1.0,
    }


__all__ = ["AnalysisFailed", "analyze_criterion", "build_result", "compute_confidence",
           "finish_analysis"]
