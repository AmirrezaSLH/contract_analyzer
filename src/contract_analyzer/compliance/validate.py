"""Structural validation of a draft against the evidence and the rules.

The API's constrained decoding guarantees the draft parses. What it cannot
say is anything that needs a second input or a second field: whether a quote
is really in the passage it names, whether the state follows from the
sub-requirement statuses, whether the question was copied rather than
paraphrased. Those are the checks here, and they are pure Python -- no model
judges the *structure*. A model judges only *content*, and that is Phase B's
evaluator, a separate concern.

Each failure is a `StructuralError(path, code, message)`. The message is
what goes back to the model in the correction turn, so it says what is
malformed and where -- **never what the answer should be**. "`relevant_quotes[2]`:
not verbatim in E4 -- copy the exact text" is the whole of the feedback.

Quote matching is deliberately forgiving of what a PDF does to text: NFKC,
curly quotes and dashes folded to ASCII, whitespace collapsed, case folded,
then a substring test. What it does not forgive is a changed word.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from ..generation.tools import Evidence
from .criteria import Criterion
from .schemas import MAX_QUOTE_CHARS, ComplianceDraft, SubRequirementStatus


@dataclass(frozen=True)
class StructuralError:
    path: str
    code: str
    message: str

    def __str__(self) -> str:
        return f"`{self.path}`: {self.message}"


_FOLD = str.maketrans(
    {
        "‘": "'", "’": "'", "‚": "'", "‛": "'",
        "“": '"', "”": '"', "„": '"', "‟": '"',
        "–": "-", "—": "-", "−": "-", " ": " ",
        "…": "...",
    }
)
_WS = re.compile(r"\s+")


def normalize_quote(text: str) -> str:
    """The form in which a quote and a passage are compared."""
    text = unicodedata.normalize("NFKC", text).translate(_FOLD)
    return _WS.sub(" ", text).strip().casefold()


def quote_in_chunk(quote: str, *texts: str) -> bool:
    needle = normalize_quote(quote)
    return bool(needle) and any(needle in normalize_quote(t) for t in texts)


def derived_state(statuses: Iterable[SubRequirementStatus]) -> str:
    """The state the sub-requirement statuses imply."""
    statuses = list(statuses)
    if statuses and all(s == "met" for s in statuses):
        return "Fully Compliant"
    if any(s in ("met", "partial") for s in statuses):
        return "Partially Compliant"
    return "Non-Compliant"


def validate_structure(
    draft: ComplianceDraft, evidence: Evidence, criterion: Criterion
) -> list[StructuralError]:
    errors: list[StructuralError] = []

    if draft.compliance_question != criterion.question:
        errors.append(
            StructuralError(
                "compliance_question", "not_verbatim",
                "must equal the question exactly as given -- copy it, do not paraphrase",
            )
        )

    # -- quotes ------------------------------------------------------------
    seen: set[tuple[str, str]] = set()
    for i, quote in enumerate(draft.relevant_quotes):
        path = f"relevant_quotes[{i}]"
        if quote.evidence_id not in evidence:
            errors.append(
                StructuralError(
                    f"{path}.evidence_id", "unknown_evidence",
                    f"{quote.evidence_id!r} is not a retrieved passage; use one of "
                    f"{', '.join(evidence.ids) or '(none retrieved)'}",
                )
            )
        elif not quote_in_chunk(
            quote.text, evidence.get(quote.evidence_id).chunk.text_for_model(),
            evidence.get(quote.evidence_id).chunk.content,
        ):
            errors.append(
                StructuralError(
                    f"{path}.text", "not_verbatim",
                    f"not verbatim in {quote.evidence_id} -- copy the exact text",
                )
            )
        if len(quote.text) > MAX_QUOTE_CHARS:
            errors.append(
                StructuralError(
                    f"{path}.text", "too_long",
                    f"{len(quote.text)} characters; at most {MAX_QUOTE_CHARS}",
                )
            )
        if not quote.text.strip():
            errors.append(StructuralError(f"{path}.text", "empty", "empty quote"))
        key = (normalize_quote(quote.text), quote.evidence_id)
        if key in seen:
            errors.append(StructuralError(path, "duplicate", "duplicates an earlier quote"))
        seen.add(key)

    # -- sub-requirements --------------------------------------------------
    expected = criterion.sub_requirement_ids()
    got = [s.id for s in draft.sub_requirements]
    if sorted(got) != sorted(expected):
        errors.append(
            StructuralError(
                "sub_requirements", "ids",
                f"must contain exactly these ids, once each: {', '.join(expected)}",
            )
        )
    n_quotes = len(draft.relevant_quotes)
    for i, sub in enumerate(draft.sub_requirements):
        path = f"sub_requirements[{i}]"
        bad = [q for q in sub.quote_indexes if not 0 <= q < n_quotes]
        if bad:
            errors.append(
                StructuralError(
                    f"{path}.quote_indexes", "out_of_range",
                    f"{bad} are not indexes into relevant_quotes (0..{n_quotes - 1})",
                )
            )
        if sub.status in ("met", "partial") and not sub.quote_indexes:
            errors.append(
                StructuralError(
                    f"{path}.quote_indexes", "needs_quote",
                    f"status {sub.status!r} needs at least one supporting quote index",
                )
            )
        if sub.status in ("missing", "not_determined") and sub.quote_indexes:
            errors.append(
                StructuralError(
                    f"{path}.quote_indexes", "unexpected_quote",
                    f"status {sub.status!r} must have no quote indexes",
                )
            )

    # -- cross-field -------------------------------------------------------
    if sorted(got) == sorted(expected):
        implied = derived_state(s.status for s in draft.sub_requirements)
        if draft.compliance_state != implied:
            errors.append(
                StructuralError(
                    "compliance_state", "inconsistent",
                    "does not follow from the sub-requirement statuses (all met = Fully; "
                    "none met or partial = Non-Compliant; otherwise Partially)",
                )
            )

    if not draft.rationale.strip():
        errors.append(StructuralError("rationale", "empty", "must not be empty"))
    if not 0.0 <= draft.raw_confidence <= 1.0:
        errors.append(StructuralError("raw_confidence", "range", "must be between 0 and 1"))
    return errors


__all__ = [
    "StructuralError",
    "derived_state",
    "normalize_quote",
    "quote_in_chunk",
    "validate_structure",
]
