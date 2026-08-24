"""The five criteria, the result schema, and the validator between them.

`criteria.json` is the assignment's list with one addition: each criterion's
prose is split into named sub-requirements, so the model judges each one and
the overall state is *derived* from those judgements rather than asserted.
`schemas.py` is what the model fills in (`ComplianceDraft`) and what a
surface receives (`ComplianceResult`); `validate.py` checks the draft
against the evidence ledger and the cross-field rules a JSON schema cannot
express. `report.py` is the layer above one criterion: five of them in
parallel, and the `AnalysisReport` a CLI writes and an API returns unchanged.
See docs/compliance.md.
"""

from .criteria import Criterion, SubRequirement, get_criteria, get_criterion
from .report import AnalysisReport, AnalysisTotals, analyze_document
from .schemas import (
    ComplianceDraft,
    ComplianceResult,
    ComplianceState,
    Quote,
    ResolvedQuote,
    SubRequirementResult,
    SubRequirementStatus,
)
from .validate import StructuralError, normalize_quote, validate_structure

__all__ = [
    "AnalysisReport",
    "AnalysisTotals",
    "ComplianceDraft",
    "ComplianceResult",
    "ComplianceState",
    "Criterion",
    "Quote",
    "ResolvedQuote",
    "StructuralError",
    "SubRequirement",
    "SubRequirementResult",
    "SubRequirementStatus",
    "analyze_document",
    "get_criteria",
    "get_criterion",
    "normalize_quote",
    "validate_structure",
]
