"""The Evaluator: the critic that sees the quotes and the claims, and nothing else.

The one failure the Analyzer structurally cannot catch is a **verbatim quote
that does not support the claim**. "Supplier *may* rotate passwords" is
copied exactly from the contract, passes every rule in `validate.py`, and
supports nothing. A second pass by the same model over the same conversation
would make the same mistake for the same reason -- it would be re-reading its
own reasoning. So the critic is handed the quotes, the passages they came
from and the claims, with no conversation and no retrieval, and has to
re-derive the support link from scratch. `EvaluationRequest` has nowhere to
put the rest, which is what makes that blindness structural rather than a
convention (see `compliance/schemas.py`).

Two stages.

**E1, deterministic (`precheck`).** Python checks what Python can check
exactly. *Search coverage*: for each sub-requirement the analyst called
`missing`, did any tool call in the run actually look for it? That is a fact
about the log, not a judgement, so it is reported whether or not the critic
call ever succeeds. *Hedge lexicon*: quotes carrying "commercially reasonable
efforts", "where feasible", "may" under a `met` status are flagged **for the
critic to examine** -- a flag, not an error, because sometimes the hedge sits
in a subordinate clause and only a reader can tell.

**E2, the critic call.** One structured call on the same client and the same
transport as everything else, `output_config.format` on the findings schema,
no tools. What comes back is parsed and then *checked in Python* -- indexes in
range, ids known, confidence in [0, 1] -- because constrained decoding
guarantees the shape and nothing else, and findings that point at quote 7 of
a three-quote draft are not findings.

**Failure is graceful, and it is one-directional.** Transport failures --
connection, timeout, 429, 5xx -- are already retried with exponential
backoff by `http_client.RetryingTransport`, the process's single retry loop;
this module does not add a second one for them. What it does retry is
*semantic* failure: a truncated answer, a refusal, JSON that does not parse,
findings that fail the deterministic checks. Those get two more attempts with
full-jitter exponential backoff (`backoff_delay`, the same curve, imported
rather than reimplemented) because the ones that are load-shaped clear when
the load does. When the ladder is exhausted, `EvaluationFailed` is raised and
the Router degrades the criterion to `verdict="unevaluated"`: the analysis
ships, flagged. **The Evaluator may only ever lower what ships. It must never
be the reason nothing ships.**
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ..compliance.criteria import Criterion
from ..compliance.schemas import EvaluationRequest, EvaluatorFindings
from ..compliance.validate import StructuralError, normalize_quote
from ..config import Settings, get_settings
from ..http_client import backoff_delay
from ..logger import get_logger, span
from .agent import call_model, text_of
from .client import Usage, get_client
from .prompts import get_prompts

log = get_logger(__name__)

#: The first attempt plus two retries. Semantic failures only -- the transport
#: has already run its own ladder underneath this one.
CRITIC_ATTEMPTS = 3

#: Small on purpose. A structural violation will not fix itself in the wait;
#: a truncation under load might. Two retries cost at most ~1.5 s, against the
#: alternative of shipping a criterion nobody criticised.
CRITIC_BACKOFF_BASE = 0.5

#: Language that grants an obligation with one hand and takes it back with the
#: other. Flagged under a `met` status for the critic to read properly -- the
#: lexicon cannot tell a carve-out from a subordinate clause, and does not try.
HEDGE_TERMS: tuple[str, ...] = (
    "commercially reasonable",
    "reasonable efforts",
    "reasonable endeavours",
    "reasonable endeavors",
    "best efforts",
    "best endeavours",
    "best endeavors",
    "where feasible",
    "where practicable",
    "as appropriate",
    "industry standard",
    "industry best practice",
    "endeavour",
    "endeavor",
    "may",
    "should",
    "from time to time",
    "substantially",
    "materially",
)

#: Words too common to mean anything as evidence that a search covered a topic.
_STOPWORDS = frozenset({
    "a", "an", "and", "any", "are", "as", "at", "be", "been", "by", "each", "every",
    "for", "from", "has", "have", "in", "is", "it", "its", "least", "must", "not",
    "of", "on", "or", "shall", "that", "the", "their", "there", "this", "to", "via",
    "with", "within",
    # Words every clause in every contract contains, so a query containing one
    # is not evidence that the query went looking for anything in particular.
    "contract", "supplier", "vendor", "customer", "party", "parties", "agreement",
})

_WORD = re.compile(r"[a-z0-9]+")


class EvaluationFailed(RuntimeError):
    """The critic could not be made to answer usably. The Router degrades; it
    does not propagate -- a criterion nobody criticised still ships, flagged."""


@dataclass
class Evaluation:
    """What one evaluation produced: the findings, and what they cost.

    `request` is the *enriched* request -- the one the critic actually saw,
    with the E1 facts filled in. The Router logs it, and it is exactly the
    JSON an out-of-process critic would have received.
    """

    findings: EvaluatorFindings
    request: EvaluationRequest
    model: str
    usage: Usage = field(default_factory=Usage)
    attempts: int = 1

    @property
    def cost_usd(self) -> float:
        return self.usage.cost(self.model)


# ==========================================================================
# E1 -- deterministic pre-checks
# ==========================================================================


def hedge_terms(text: str) -> list[str]:
    """Hedging language in a quote, in the order the lexicon lists it."""
    haystack = normalize_quote(text)
    found = []
    for term in HEDGE_TERMS:
        # Word-bounded: "may" must not fire on "maybe", and "materially" must
        # not be found twice via "materially" and "material".
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", haystack):
            found.append(term)
    return found


def _stem(word: str) -> str:
    """Enough stemming to make "rotated", "rotation" and "rotate" the same word.

    A criterion says "passwords are rotated every 90 days" and the model
    searches for "password rotation policy". Comparing surface forms would
    call that unsearched, which is a false accusation dressed as a fact. This
    is four suffix rules, not a linguistics library: the check is coarse by
    design and only has to be right about whether a search *happened*.
    """
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    for suffix in ("ing", "ion", "ed"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    if len(word) > 4 and word.endswith("e"):
        return word[:-1]
    return word


def content_terms(text: str) -> set[str]:
    """The stemmed content words of a string -- what two texts can be said to
    share. Public because the Router's cross-criterion pass compares quotes
    against sub-requirements with the same notion of "mentions"."""
    return {
        _stem(w)
        for w in _WORD.findall(text.casefold())
        if w not in _STOPWORDS and len(w) > 2
    }


def distinctive_terms(
    requirement: str, sub_requirements: Sequence[Any], sub_requirement_id: str
) -> set[str]:
    """The words that separate this sub-requirement from its siblings.

    Plain token overlap does not work here, and finding that out is the point.
    Every sub-requirement of a password criterion contains "password", so any
    query at all would "cover" all five of them and the check could never
    fire. What distinguishes *vaulting* from *rotation* is the word
    "vaulting" -- so the criterion's own requirement text and the other
    sub-requirements are subtracted, and what remains is what a search would
    have had to go after to have looked for this one specifically.

    Falls back to the sub-requirement's full vocabulary when subtraction
    leaves nothing: a sub-requirement phrased entirely in its siblings' words
    is one this check cannot speak about, and a silent `False` there would be
    an accusation rather than a fact.
    """
    per_sub = {sub.id: content_terms(sub.requirement) for sub in sub_requirements}
    mine = per_sub.get(sub_requirement_id, set())
    siblings: set[str] = set()
    for other_id, tokens in per_sub.items():
        if other_id != sub_requirement_id:
            siblings |= tokens
    return (mine - siblings - content_terms(requirement)) or mine


def searched_for(terms: set[str], queries: list[str]) -> bool:
    """Did any query in the run go after these terms? One word is enough.

    Deliberately generous once the terms are distinctive: the check exists to
    catch a `missing` verdict reached without looking at all, not to grade the
    quality of the search. A strict version would flag good runs, and a flag
    that fires on good runs is a flag everyone learns to ignore.
    """
    if not terms:
        return True
    return any(terms & content_terms(query) for query in queries)


def unsearched(criterion: Criterion, request: EvaluationRequest) -> list[str]:
    """Sub-requirement ids called `missing` that no query in the run went after.

    A fact about the log, which is why it survives a failed critic call: the
    Router receives it whether or not the model ever answered.
    """
    return [
        sub.id
        for sub in request.sub_requirements
        if sub.status == "missing"
        and not searched_for(
            distinctive_terms(criterion.requirement, criterion.sub_requirements, sub.id),
            request.searched_queries,
        )
    ]


def precheck(request: EvaluationRequest, criterion: Criterion) -> EvaluationRequest:
    """The E1 facts, attached to the request the critic will see.

    Pure: returns an enriched copy rather than mutating, so the Router's
    original stays exactly what the Router built.
    """
    with span("evaluator.precheck", log, criterion=request.criterion_id) as bag:
        met_or_partial = {
            index
            for sub in request.sub_requirements
            if sub.status in ("met", "partial")
            for index in sub.quote_indexes
        }
        quotes = [
            quote.model_copy(
                update={
                    "hedge_terms": hedge_terms(quote.text) if quote.index in met_or_partial else []
                }
            )
            for quote in request.quotes
        ]
        gaps = unsearched(criterion, request)
        bag.update(
            hedged_quotes=sum(1 for q in quotes if q.hedge_terms),
            unsearched=len(gaps),
            unsearched_ids=gaps,
        )
    return request.model_copy(update={"quotes": quotes, "unsearched": gaps})


# ==========================================================================
# Deterministic checks on what the critic returned
# ==========================================================================


def validate_findings(
    findings: EvaluatorFindings, request: EvaluationRequest
) -> list[StructuralError]:
    """Structural sanity of the findings themselves.

    Constrained decoding guarantees the shape -- the keys, the types, the
    enums -- and nothing about whether the values refer to anything. Findings
    that judge quote 7 of a three-quote draft, or a sub-requirement this
    criterion does not have, are not findings; they count as a failed attempt.
    """
    errors: list[StructuralError] = []
    n_quotes = len(request.quotes)
    known = {sub.id for sub in request.sub_requirements}

    for i, judged in enumerate(findings.quote_support):
        path = f"quote_support[{i}]"
        if not 0 <= judged.quote_index < n_quotes:
            errors.append(
                StructuralError(
                    f"{path}.quote_index", "out_of_range",
                    f"{judged.quote_index} is not an index into quotes (0..{n_quotes - 1})",
                )
            )
        if judged.sub_requirement_id not in known:
            errors.append(
                StructuralError(
                    f"{path}.sub_requirement_id", "unknown_sub_requirement",
                    f"{judged.sub_requirement_id!r} is not one of {', '.join(sorted(known))}",
                )
            )

    seen: set[str] = set()
    for i, agreement in enumerate(findings.status_agreement):
        path = f"status_agreement[{i}]"
        if agreement.sub_requirement_id not in known:
            errors.append(
                StructuralError(
                    f"{path}.sub_requirement_id", "unknown_sub_requirement",
                    f"{agreement.sub_requirement_id!r} is not one of {', '.join(sorted(known))}",
                )
            )
        elif agreement.sub_requirement_id in seen:
            errors.append(
                StructuralError(path, "duplicate", "judges a sub-requirement already judged")
            )
        seen.add(agreement.sub_requirement_id)

    unknown = [s for s in findings.missing_searches if s not in known]
    if unknown:
        errors.append(
            StructuralError(
                "missing_searches", "unknown_sub_requirement",
                f"{unknown} are not sub-requirements of this criterion",
            )
        )
    if not 0.0 <= findings.critic_confidence <= 1.0:
        errors.append(
            StructuralError("critic_confidence", "range", "must be between 0 and 1")
        )
    return errors


# ==========================================================================
# E2 -- the critic call
# ==========================================================================


def evaluate(
    request: EvaluationRequest,
    criterion: Criterion,
    *,
    settings: Settings | None = None,
    client: Any = None,
    sleep=time.sleep,
) -> Evaluation:
    """Judge one drafted assessment. Raises `EvaluationFailed`; never blocks.

    `sleep` is injected so the retry ladder can be tested without waiting for
    it -- the delays themselves are asserted, not endured.
    """
    settings = settings or get_settings()
    client = client or get_client(settings)
    enriched = precheck(request, criterion)
    findings, usage, attempts = _critic(enriched, settings=settings, client=client, sleep=sleep)

    # The E1 gaps are facts about the log. The critic may add to them from its
    # own reading; it does not get to remove one by not mentioning it.
    merged = sorted(set(findings.missing_searches) | set(enriched.unsearched))
    if merged != findings.missing_searches:
        findings = findings.model_copy(update={"missing_searches": merged})
    return Evaluation(
        findings=findings,
        request=enriched,
        model=settings.evaluator_model,
        usage=usage,
        attempts=attempts,
    )


def _critic(
    request: EvaluationRequest, *, settings: Settings, client: Any, sleep
) -> tuple[EvaluatorFindings, Usage, int]:
    """The call, its deterministic checks, and the backoff ladder around both."""
    prompts = get_prompts(settings)
    system = prompts.get("evaluator.system")
    user = prompts.format("evaluator.user", request=_render(request))
    statuses = {sub.id: sub.status for sub in request.sub_requirements}
    usage = Usage()
    reason = "never attempted"

    with span(
        "evaluator.critic", log, criterion=request.criterion_id, model=settings.evaluator_model,
        round=request.round,
    ) as bag:
        for attempt in range(CRITIC_ATTEMPTS):
            if attempt:
                wait = backoff_delay(attempt - 1, base=CRITIC_BACKOFF_BASE)
                log.warning(
                    "evaluator.retry",
                    extra={"criterion": request.criterion_id, "attempt": attempt + 1,
                           "max_attempts": CRITIC_ATTEMPTS, "wait_s": round(wait, 2),
                           "reason": reason},
                )
                sleep(wait)
            message = call_model(
                client,
                model=settings.evaluator_model,
                max_tokens=settings.evaluator_max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                effort=settings.evaluator_effort,
                surface="evaluator",
                turn=attempt + 1,
                format=EvaluatorFindings.output_format(),
            )
            usage += Usage.from_message(message)
            findings, reason = _parse(message, request)
            if findings is not None:
                bag.update(
                    attempts=attempt + 1,
                    state_agreement=findings.state_agreement,
                    critic_confidence=findings.critic_confidence,
                    disputed_quotes=len(findings.disputed_quotes),
                    disputed_statuses=len(findings.disputed_statuses),
                    missing_searches=len(findings.missing_searches),
                    support_ratio=round(findings.support_ratio(statuses), 3),
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cost_usd=usage.cost(settings.evaluator_model),
                )
                return findings, usage, attempt + 1
        bag.update(attempts=CRITIC_ATTEMPTS, failed=reason,
                   cost_usd=usage.cost(settings.evaluator_model))
    raise EvaluationFailed(
        f"the critic did not answer usably for {request.criterion_id} after "
        f"{CRITIC_ATTEMPTS} attempts: {reason}"
    )


def _parse(message: Any, request: EvaluationRequest) -> tuple[EvaluatorFindings | None, str]:
    """The findings, or None and one line saying what was wrong with the answer."""
    if message.stop_reason in ("max_tokens", "refusal"):
        return None, f"stopped with {message.stop_reason!r}"
    try:
        findings = EvaluatorFindings.model_validate_json(text_of(message))
    except ValidationError as exc:
        return None, f"not valid findings: {exc.errors()[0].get('msg', exc.errors()[0])}"
    errors = validate_findings(findings, request)
    if errors:
        return None, "; ".join(str(e) for e in errors)
    return findings, ""


def _render(request: EvaluationRequest) -> str:
    """The request as JSON, which is literally what goes over the seam.

    Not a prose rendering: the day this call runs out of process, the body is
    this string, and a demo can pull the same JSON off the `router.decision`
    span and diff it.
    """
    return json.dumps(request.model_dump(mode="json"), indent=2, ensure_ascii=False)


__all__ = [
    "CRITIC_ATTEMPTS",
    "HEDGE_TERMS",
    "Evaluation",
    "EvaluationFailed",
    "content_terms",
    "distinctive_terms",
    "evaluate",
    "hedge_terms",
    "precheck",
    "searched_for",
    "unsearched",
    "validate_findings",
]
