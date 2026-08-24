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
from ..compliance.validate import quote_in_chunk
from ..config import RetrievalMode
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
    hint: str | None = Field(default=None, examples=["Call GET /api/documents to list ids."])


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
    #: The model the compliance analysis uses. Independent of `answer_model`.
    analysis_model: str
    #: The models `POST /chat` will accept. A client renders its picker from
    #: this rather than from a list of its own, so the choices it offers are
    #: exactly the choices that will be honoured.
    chat_models: list[str] = Field(default_factory=list)
    #: The retrieval defaults a client should show as selected before anyone
    #: touches a control.
    retrieval_mode: str = "hybrid"
    retrieval_top_k: int = 6
    #: `api_max_upload_mb`, so an uploader can refuse a file the same way the
    #: API would rather than discovering the 413 after the bytes are sent.
    max_upload_mb: float = 25.0
    #: The pool shape: analyses in flight, and criteria in parallel inside one.
    #: A UI states this on a running analysis, and one that hardcodes it states
    #: it wrongly the first time someone tunes it.
    api_workers: int = 2
    analysis_workers: int = 5
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

#: `interrupted` is not a flavour of `failed`: it is what a row left `running`
#: by a process that went away reads as after the next startup reconciles it.
#: The model refusing and the machine going away want different KPI treatment
#: and different copy -- "this analysis failed" against "this analysis was
#: interrupted; run it again".
JobStatus = Literal["queued", "running", "done", "failed", "cancelled", "interrupted"]


class LastAnalysisOut(BaseModel):
    """The newest analysis of a document, as a list row needs it.

    `states` is a count per compliance state rather than a summary sentence:
    a library composes "5 of 5 compliant" or "2 gaps found" in its own words,
    and the next consumer will want different ones.
    """

    analysis_id: str
    status: JobStatus
    completed_at: str | None = None
    states: dict[str, int] = Field(default_factory=dict)
    needs_review: int = 0


class DocumentOut(BaseModel):
    document_id: int
    filename: str
    pages: int | None = None
    chunks: int = 0
    #: `outline`, `headings` or `none` -- whether the section breadcrumbs were
    #: read from the PDF or inferred. A reviewer checking a citation wants it.
    spine_source: str = "none"
    ingested_at: str = ""
    #: `null` when nothing has been run against this document. That is what
    #: drives a library's "Not analysed" row and an analysis view's empty
    #: state, and it costs one query for the whole list rather than one per
    #: row -- see `analyses.last_analysis_by_document`.
    last_analysis: LastAnalysisOut | None = None


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



class AnalyzeRequest(BaseModel):
    document_id: int = Field(description="The document to analyse. Required: there is no "
                                         "implicit current document.")
    criteria: list[str] | None = Field(
        default=None, description="Criterion ids to run. Omit for all five."
    )


class CriterionProgress(BaseModel):
    """One line of the progress table, filled in as the criterion finishes.

    `running` is set the moment a criterion first reaches for the contract, not
    when a worker picks it up: at `analysis_workers >= 5` all five start at once
    and "queued" would be true of none of them for more than a moment. It is
    what a progress view draws as the row in flight.
    """

    id: str
    status: Literal["queued", "running", "done", "skipped", "failed"] = "queued"
    state: ComplianceState | None = None
    confidence: float | None = None
    needs_review: bool | None = None
    #: Wall-clock seconds, once this criterion is `done`. The five run in
    #: parallel, so these do not sum to `totals.latency_s`.
    latency_s: float | None = None


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

    # The three per-question overrides. All optional; omitting one means the
    # configured default. They are per *question* rather than per session
    # because this API holds no session -- see `history`.
    model: str | None = Field(
        default=None,
        description="Answer model for this question. Must be one of `chat_models` from "
                    "GET /api/health; omit for the configured `answer_model`.",
        examples=["claude-sonnet-5"],
    )
    retrieval_mode: RetrievalMode | None = Field(
        default=None,
        description="What the search tool does when the model does not choose a mode. "
                    "The model can still override it per call.",
    )
    top_k: int | None = Field(
        default=None, ge=1, le=20,
        description="Passages per search. Omit for the configured `retrieval_top_k`.",
    )


class CitationOut(BaseModel):
    """A citation resolved to everything needed to check it by hand.

    The field names are `ResolvedQuote`'s -- `text`, `section_ref`, `verified`
    -- and deliberately so: a client renders a quote from an analysis report
    and a quote from a chat answer with the same card, and two names for
    "which clause this came from" would buy that client nothing but a
    translation layer.
    """

    evidence_id: str
    text: str
    section_ref: str
    page_display: str = ""
    chunk_id: int | None = None
    #: Whether `text` was found verbatim in the passage it names. Quotes here
    #: are extracted by the model API from the passages we sent, so this is a
    #: check of that guarantee rather than of the model's honesty -- and it
    #: means the same thing as it does on a report's quote.
    verified: bool = True
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
                text=c.quote,
                section_ref=c.title,
                page_display=c.page_display,
                chunk_id=c.entry.chunk.chunk_id,
                # Checked rather than asserted. The citations API extracts the
                # text from the document blocks we sent, so this should always
                # hold -- which is exactly why it is worth measuring: a quote
                # that fails here is a bug in the block we built, and a client
                # that shows `verified` on a report must be told the truth on
                # an answer too.
                verified=quote_in_chunk(
                    c.quote, c.entry.chunk.text_for_model(), c.entry.chunk.content
                ),
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
    "JobStatus",
    "LastAnalysisOut",
    "Message",
    "Progress",
    "SectionOut",
    "UploadOut",
    "answer_of",
    "as_dict",
    "summarise_report",
]
