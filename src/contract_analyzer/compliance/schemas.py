"""What the model fills in, and what a surface receives.

`ComplianceDraft` is the structured-output schema. The API's constrained
decoding guarantees it *parses* -- keys, types, the state enum -- so nothing
here re-checks that. No field carries a numeric or length constraint either:
structured outputs do not enforce them, and a rule that is not enforced is a
rule that belongs in `validate.py`, where it can name what it found.

`ComplianceResult` is the draft after validation, with each quote resolved
to the passage it cites, the derived confidence and its components, and the
run's bookkeeping. Every quote in a result is verbatim in the chunk it
names, or the result says `needs_review`.

The rest of this module is the **three-agent protocol** (`docs/agents/`):
`EvaluationRequest` is what the Router hands the Evaluator, `EvaluatorFindings`
what comes back, `RevisionRequest` what the Router then asks the Analyzer for.
They live here, beside the result they judge, because they are part of the
same wire format -- and because a message type in the module that owns the
schema cannot drift from it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ComplianceState = Literal["Fully Compliant", "Partially Compliant", "Non-Compliant"]
SubRequirementStatus = Literal["met", "partial", "missing", "not_determined"]

#: How long a quote may be. A quote is a pointer into a clause, not the clause.
MAX_QUOTE_CHARS = 300


class _Strict(BaseModel):
    # `additionalProperties: false` and every field required -- what the
    # structured-output grammar wants, and what makes a draft unambiguous.
    model_config = ConfigDict(extra="forbid")

    @classmethod
    def output_format(cls) -> dict[str, Any]:
        """The `output_config.format` value for this schema."""
        return {"type": "json_schema", "schema": cls.model_json_schema()}


class Quote(_Strict):
    text: str = Field(description="Copied verbatim from one retrieved passage.")
    evidence_id: str = Field(description="The passage's evidence id, e.g. E3.")


class SubRequirementResult(_Strict):
    id: str = Field(description="The sub-requirement id exactly as listed.")
    requirement: str
    status: SubRequirementStatus
    quote_indexes: list[int] = Field(
        description="Indexes into relevant_quotes that support this status."
    )


class ComplianceDraft(_Strict):
    compliance_question: str = Field(description="The question, copied exactly as given.")
    compliance_state: ComplianceState
    sub_requirements: list[SubRequirementResult]
    relevant_quotes: list[Quote]
    rationale: str
    raw_confidence: float = Field(
        description="Your own estimate, 0 to 1, that compliance_state is correct."
    )


class ResolvedQuote(BaseModel):
    text: str
    evidence_id: str
    section_ref: str
    page_display: str
    chunk_id: int | None
    #: True when `text` was found verbatim in the passage it names.
    verified: bool


# ==========================================================================
# The agent protocol: Router <-> Evaluator, Router -> Analyzer
# ==========================================================================
# Every message between the three agents is one of these models. They are
# Pydantic because the hand-off has to be inspectable: today it is a function
# call in one process, and the JSON on the span is exactly what an
# out-of-process evaluator -- a second vendor, a batch re-scoring job --
# would receive. The seam is real even while the call is local.


class QuoteSupport(_Strict):
    """The critic's reading of one quote against one sub-requirement."""

    quote_index: int = Field(description="Index into relevant_quotes, as given.")
    sub_requirement_id: str = Field(description="The sub-requirement id this quote was cited for.")
    support: Literal["supports", "partial", "irrelevant", "contradicts"] = Field(
        description=(
            "supports: the quote obliges what the sub-requirement asks. "
            "partial: it addresses it with a gap, a carve-out or hedged wording. "
            "irrelevant: it is about something else, however verbatim. "
            "contradicts: it says the opposite of the claim."
        )
    )
    note: str = Field(description="One sentence saying why.")


class StatusAgreement(_Strict):
    """Whether the analyst's status for one sub-requirement follows from its quotes."""

    sub_requirement_id: str
    agreement: Literal["agree", "too_strong", "too_weak"]
    note: str = Field(description="One sentence saying why.")


class EvaluatorFindings(_Strict):
    """What the Evaluator returns: evidence, never authority.

    No field here says what happens next. The Router reads these findings and
    decides; the Analyzer never sees them raw. `critic_confidence` is the
    critic's own estimate of the state, independent of the analyst's, and
    enters the composed confidence through `min()`.
    """

    quote_support: list[QuoteSupport]
    status_agreement: list[StatusAgreement]
    state_agreement: Literal["agree", "disagree"]
    missing_searches: list[str] = Field(
        description="Sub-requirement ids marked missing that were never searched for."
    )
    critic_confidence: float = Field(
        description="Your own estimate, 0 to 1, that compliance_state is correct."
    )
    notes: str = Field(description="Anything the fields above cannot carry. May be empty.")

    @property
    def disputed_quotes(self) -> list[QuoteSupport]:
        """Quotes the critic judged not to support what they were cited for."""
        return [q for q in self.quote_support if q.support in ("irrelevant", "contradicts")]

    @property
    def disputed_statuses(self) -> list[StatusAgreement]:
        return [s for s in self.status_agreement if s.agreement != "agree"]

    @property
    def support_score(self) -> float:
        """How much of what was claimed the critic found actually supported.

        One judgement per (quote, sub-requirement) pair, so a quote cited for
        two sub-requirements is judged twice and counts twice -- the unit here
        is a claim, not a string. `partial` scores a half: a hedged quote is
        evidence, just not whole evidence, and scoring it zero would punish an
        analyst who correctly answered `partial` with correctly partial
        language.
        """
        return sum(
            1.0 if q.support == "supports" else 0.5 if q.support == "partial" else 0.0
            for q in self.quote_support
        )

    @property
    def support_ratio(self) -> float:
        """`support_score` over the claims judged; 1.0 when nothing was claimed."""
        return self.support_score / len(self.quote_support) if self.quote_support else 1.0


class CitedPassage(BaseModel):
    """One retrieved passage, as the critic sees it: the text and where it came from.

    Only passages the draft's quotes actually cite are ever built into a
    request. The critic gets the evidence, never the reasoning that used it.
    """

    evidence_id: str
    section_path: str
    page: str
    text: str


class ClaimedQuote(BaseModel):
    """A draft quote as the critic sees it, with the E1 hedge flags attached."""

    index: int
    text: str
    evidence_id: str
    #: Hedge terms found in this quote by the deterministic pre-check. A flag
    #: for the critic to examine, not a verdict: sometimes the hedge sits in a
    #: subordinate clause and only a reader can tell.
    hedge_terms: list[str] = Field(default_factory=list)


class EvaluationRequest(BaseModel):
    """Router -> Evaluator. Built by `generation/router.py`, never by the Analyzer.

    This object *is* the isolation guarantee: it has no field for the
    conversation, the thinking, or the uncited half of the ledger, so an
    Evaluator cannot inherit the reasoning that made the error even by
    accident.
    """

    criterion_id: str
    requirement: str
    question: str
    compliance_state: ComplianceState
    sub_requirements: list[SubRequirementResult]
    quotes: list[ClaimedQuote]
    passages: list[CitedPassage]
    rationale: str
    #: E1 facts about the log, which only the Router can see.
    searched_queries: list[str] = Field(default_factory=list)
    unsearched: list[str] = Field(
        default_factory=list,
        description="Sub-requirement ids marked missing with no search that overlaps them.",
    )
    round: int = 0


class RevisionRequest(BaseModel):
    """Router -> Analyzer, rounds >= 1. Names the defect, never the answer.

    `instructions` follow the same contract as `analysis.fix_structure`'s
    feedback: what is wrong and where. "relevant_quotes[1] does not support
    sub-requirement mfa" is the whole of it. Saying what the answer should be
    would make the next draft the Router's, not the Analyzer's.
    """

    mode: Literal["redraft", "research"]
    round: int
    instructions: list[str]
    reason_codes: list[str] = Field(default_factory=list)

    def text(self) -> str:
        return "\n".join(f"- {line}" for line in self.instructions)


class ComplianceResult(BaseModel):
    criterion_id: str
    compliance_requirement: str
    compliance_question: str
    compliance_state: ComplianceState
    sub_requirements: list[SubRequirementResult]
    relevant_quotes: list[ResolvedQuote]
    rationale: str
    raw_confidence: float
    confidence: float
    #: The three terms the confidence was computed from, so a later design
    #: can be fitted without changing the schema.
    confidence_components: dict[str, float]
    needs_review: bool
    #: Structural errors that survived every correction round, verbatim.
    unresolved_errors: list[str] = Field(default_factory=list)
    structure_rounds: int
    ended_by: str
    tool_calls: int
    usage: dict[str, int]
    cost_usd: float
    model: str
    #: Wall-clock seconds for this criterion alone -- the agent loop plus the
    #: structured finisher. Defaulted rather than required so a report written
    #: before this field existed still parses; a run that produced one always
    #: sets it. The five criteria run in parallel, so these do not sum to the
    #: run's `totals.latency_s` and are not meant to.
    latency_s: float = 0.0
    #: What the Router decided after the Evaluator spoke. `accept`: the critic
    #: found nothing open. `fallback`: rounds ran out with findings still open.
    #: `unevaluated`: the Evaluator itself failed, and the analysis ships
    #: flagged rather than not at all. Defaulted to `unevaluated` so a report
    #: written before the Evaluator existed parses and reads honestly -- it
    #: was, in fact, not evaluated.
    verdict: Literal["accept", "fallback", "unevaluated"] = "unevaluated"
    #: Revision rounds the Router spent after the first analysis. 0 means the
    #: first draft was accepted (or was never revised).
    rounds: int = 0
    #: The critic's findings, stored whatever the Router decided -- including
    #: on `accept`, so the UI can show why the confidence is what it is and
    #: the KPI page computes agreement rates from results rather than logs.
    evaluator_findings: EvaluatorFindings | None = None

    @property
    def confidence_bucket(self) -> str:
        return confidence_bucket(self.confidence)


def confidence_bucket(value: float) -> str:
    """High / Medium / Low, the labels the UI shows beside the number."""
    if value >= 0.75:
        return "High"
    if value >= 0.5:
        return "Medium"
    return "Low"


__all__ = [
    "MAX_QUOTE_CHARS",
    "CitedPassage",
    "ClaimedQuote",
    "ComplianceDraft",
    "ComplianceResult",
    "ComplianceState",
    "EvaluationRequest",
    "EvaluatorFindings",
    "Quote",
    "QuoteSupport",
    "ResolvedQuote",
    "RevisionRequest",
    "StatusAgreement",
    "SubRequirementResult",
    "SubRequirementStatus",
    "confidence_bucket",
]
