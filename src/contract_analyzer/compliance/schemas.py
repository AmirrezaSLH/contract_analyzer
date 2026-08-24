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

    @classmethod
    def output_format(cls) -> dict[str, Any]:
        """The `output_config.format` value for this schema."""
        return {"type": "json_schema", "schema": cls.model_json_schema()}


class ResolvedQuote(BaseModel):
    text: str
    evidence_id: str
    section_ref: str
    page_display: str
    chunk_id: int | None
    #: True when `text` was found verbatim in the passage it names.
    verified: bool


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
    "ComplianceDraft",
    "ComplianceResult",
    "ComplianceState",
    "Quote",
    "ResolvedQuote",
    "SubRequirementResult",
    "SubRequirementStatus",
    "confidence_bucket",
]
