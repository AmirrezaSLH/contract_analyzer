"""What goes over the wire.

The rule is *reuse, do not mirror*. `ComplianceResult` and `AnalysisReport` are
already pydantic models and travel unchanged -- the report the CLI writes to
disk is byte-for-byte the report `GET /analyses/{id}` returns, which is what
keeps a second schema from drifting away from the first. `Criterion` and
`Document` are dataclasses, which pydantic serialises as they are.

One type genuinely needs projecting. `AnswerResult` holds *live objects* -- an
`Evidence` ledger, `ToolCall` records, a `Usage` -- that exist to be used inside
one run, not to be read by a client. `Answer` is its wire form: the text, the
citations resolved to what a reviewer needs to check them, and the usage.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from ..compliance.schemas import ComplianceState
from ..generation.chat import AnswerResult
from ..report import AnalysisReport, AnalysisTotals

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ErrorBody(BaseModel):
    #: Stable across versions, and the thing to branch on. A model reading an
    #: MCP tool result can act on `document_not_found`; it cannot act on 404.
    code: str = Field(examples=["document_not_found"])
    message: str
    #: What to do next, in one sentence.
    hint: str | None = Field(default=None, examples=["Call GET /documents to list ids."])


class Error(BaseModel):
    """The body of every 4xx and 5xx this API returns."""

    error: ErrorBody


# --------------------------------------------------------------------------
# Health and reference
# --------------------------------------------------------------------------


class Health(BaseModel):
    status: Literal["ok", "degraded"] = "ok"
    version: str
    #: False when the database cannot be opened -- the one thing that makes
    #: this endpoint say `degraded`, because nothing else works without it.
    db: bool = True
    embedder: str
    embedding_model: str
    answer_model: str
    #: Whether ANTHROPIC_API_KEY is set. Analysis and chat need it; upload,
    #: retrieval and this endpoint do not.
    key_present: bool = False
    #: Whether this API demands an X-API-Key.
    auth_required: bool = False
    documents: int = 0
    analyses_running: int = 0


class SubRequirementOut(BaseModel):
    id: str
    requirement: str


class CriterionOut(BaseModel):
    """One of the five questions, as `GET /criteria` publishes it."""

    id: str
    requirement: str
    question: str
    sub_requirements: list[SubRequirementOut] = Field(default_factory=list)
    states: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------


class DocumentOut(BaseModel):
    document_id: int
    filename: str
    pages: int | None = None
    chunks: int = 0
    #: `outline`, `headings` or `none` -- whether the section breadcrumbs were
    #: read from the PDF or inferred. A reviewer checking a citation wants it.
    spine_source: str = "none"
    ingested_at: str = ""


class DocumentDetail(DocumentOut):
    """`GET /documents/{id}`: the document plus what has been run against it."""

    analyses: list[AnalysisSummary] = Field(default_factory=list)


class UploadOut(DocumentOut):
    """`POST /documents`: the same, plus what the upload cost."""

    elapsed_s: float = 0.0


class SectionOut(BaseModel):
    path: list[str] = Field(default_factory=list)
    title: str = ""
    page_display: str = ""
    chunks: int = 0


# --------------------------------------------------------------------------
# Analyses
# --------------------------------------------------------------------------

JobStatus = Literal["queued", "running", "done", "failed", "cancelled"]


class AnalyzeRequest(BaseModel):
    document_id: int = Field(description="The document to analyse. Required: there is no "
                                         "implicit current document.")
    criteria: list[str] | None = Field(
        default=None, description="Criterion ids to run. Omit for all five."
    )


class CriterionProgress(BaseModel):
    """One line of the progress table, filled in as the criterion finishes."""

    id: str
    status: Literal["queued", "running", "done", "skipped", "failed"] = "queued"
    state: ComplianceState | None = None
    confidence: float | None = None
    needs_review: bool | None = None


class Progress(BaseModel):
    done: int = 0
    total: int = 0


class AnalysisSummary(BaseModel):
    """An analysis without its report -- what a list endpoint returns."""

    analysis_id: str
    document_id: int
    status: JobStatus
    #: Human text: `queued`, `criterion 3/5`, `done`.
    stage: str = ""
    progress: Progress = Field(default_factory=Progress)
    criteria: list[CriterionProgress] = Field(default_factory=list)
    totals: AnalysisTotals | None = None
    trace_id: str | None = None
    error: str | None = None
    created_at: str = ""
    started_at: str | None = None
    completed_at: str | None = None


class Analysis(AnalysisSummary):
    """`GET /analyses/{id}`: the same, plus the report once there is one.

    `report` is present on `done`, and present but partial on `cancelled`.
    `?detail=summary` drops the quotes and the rationale -- the MCP default,
    because a full report is a lot of context to put in a model's window.
    """

    report: AnalysisReport | None = None


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    document_id: int = Field(description="The contract to answer from. Tools cannot reach "
                                         "any other document.")
    question: str = Field(min_length=1)
    history: list[Message] = Field(
        default_factory=list,
        description="The client's transcript. This API is stateless; the last 8 messages "
                    "are replayed as plain text.",
    )
    stream: bool = Field(default=True, description="Server-sent events, or one JSON body.")


class CitationOut(BaseModel):
    """A citation resolved to everything needed to check it by hand."""

    evidence_id: str
    quote: str
    title: str
    page_display: str = ""
    chunk_id: int | None = None
    #: Character offsets into the passage the quote was extracted from.
    start: int = 0
    end: int = 0


class Answer(BaseModel):
    """`POST /chat` with `stream=false`, and the shape the stream adds up to."""

    text: str
    citations: list[CitationOut] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
    cost_usd: float = 0.0
    model: str = ""
    #: The finisher's stop reason; `no_context` when the search found nothing
    #: and no answer request was made at all.
    stop_reason: str = ""
    #: `model` when it stopped asking for tools, `cap` when a counter did.
    ended_by: str = ""
    tool_calls: int = 0
    grounded: bool = True


def answer_of(result: AnswerResult) -> Answer:
    """`AnswerResult` -> the wire. The ledger and the tool records stay behind:
    they exist to be used during a run, not to be read by a client."""
    return Answer(
        text=result.text,
        citations=[
            CitationOut(
                evidence_id=c.evidence_id,
                quote=c.quote,
                title=c.title,
                page_display=c.page_display,
                chunk_id=c.entry.chunk.chunk_id,
                start=c.start,
                end=c.end,
            )
            for c in result.citations
        ],
        usage=result.usage.as_dict(),
        cost_usd=round(result.cost_usd, 6),
        model=result.model,
        stop_reason=result.stop_reason,
        ended_by=result.ended_by,
        tool_calls=len(result.tool_calls),
        grounded=result.grounded,
    )


Detail = Annotated[
    Literal["full", "summary"],
    Field(description="`summary` omits quotes and rationale from the report."),
]


def summarise_report(report: AnalysisReport) -> AnalysisReport:
    """The report with the bulky fields emptied, for `?detail=summary`.

    A copy, not a mutation: the runner's report is the one held in memory for
    every other reader of this job.
    """
    return report.model_copy(
        update={
            "results": [
                result.model_copy(update={"relevant_quotes": [], "rationale": ""})
                for result in report.results
            ]
        },
        deep=True,
    )


def as_dict(model: BaseModel) -> dict[str, Any]:
    """JSON-mode dump: what an SSE frame carries."""
    return model.model_dump(mode="json")


DocumentDetail.model_rebuild()

__all__ = [
    "Analysis",
    "AnalysisSummary",
    "AnalyzeRequest",
    "Answer",
    "ChatRequest",
    "CitationOut",
    "CriterionOut",
    "CriterionProgress",
    "Detail",
    "DocumentDetail",
    "DocumentOut",
    "Error",
    "ErrorBody",
    "Health",
    "Message",
    "Progress",
    "SectionOut",
    "UploadOut",
    "answer_of",
    "as_dict",
    "summarise_report",
]
