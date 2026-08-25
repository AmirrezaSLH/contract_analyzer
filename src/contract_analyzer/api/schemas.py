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


class SearchRequest(BaseModel):
    """`POST /documents/{id}/search`: a question, and how much of the answer.

    No `mode`. Which retriever runs is a *policy* this deployment owns -- hybrid
    where there is an embedder, keyword where there is not -- and a client that
    could ask for `vector` on a keyless deployment would be asking for a 503 it
    has no way to anticipate. `POST /chat` exposes the knob because the model
    behind it can read the mode back and try another one; a caller reading
    passages cannot.
    """

    query: str = Field(
        min_length=1,
        description="What to look for, in the words the question uses. Matched against "
                    "the contract's own vocabulary as well as its meaning.",
        examples=["password rotation and complexity requirements"],
    )
    top_k: int | None = Field(
        default=None, ge=1, le=20,
        description="Passages to return. Omit for the configured `retrieval_top_k`.",
    )


class PassageOut(BaseModel):
    """One retrieved chunk, as something to read and to cite.

    `text`, not `quote`. A quote in this API is an extraction the model API
    pulled out of a passage we sent, and `verified` says whether it survived
    the check -- see `CitationOut`. This is the passage itself, unextracted and
    unchecked, and calling it a quote would promise a guarantee it does not
    carry.
    """

    chunk_id: int
    #: The leaf section: `6.6 Password Management Standard`.
    section: str = ""
    #: The whole path, `A > B > C`, for a reader that has room for it.
    breadcrumb: str = ""
    page_display: str = ""
    element_type: str = "paragraph"
    text: str
    #: Higher is better, whatever produced it -- RRF score in hybrid mode,
    #: cosine similarity in vector, negated BM25 in keyword. Comparable within
    #: one response and meaningless across two.
    score: float = 0.0
    similarity: float | None = None


class SearchOut(BaseModel):
    """`POST /documents/{id}/search`: the passages, and what produced them.

    `mode` is echoed because it is not always the configured one: a deployment
    with no embedding key answers in `keyword`, and a caller that shows "no
    results" without knowing which retriever ran is guessing.
    """

    document_id: int
    query: str
    mode: str
    passages: list[PassageOut] = Field(default_factory=list)


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


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------
# The KPI page's four payloads, transcribed from `metrics/queries.py`. They
# are models rather than `dict[str, Any]` for one reason: `docs/openapi.json`
# is what `openapi-typescript` reads, and a handler annotated `dict[str, Any]`
# describes itself as `additionalProperties: true` -- which would make the
# dashboard the one part of the front end with no type safety at all.
#
# **The query layer is the source of truth, not this file.** Every optional
# field below is optional because `queries.py` can return `None` there, and
# the two rules that produces are worth stating once:
#
#   * **A rate is `None` when its denominator is zero, and `None` is not 0.**
#     A quote-verification rate of `null` with `quotes_total: 0` means no quote
#     was produced -- a different and more alarming fact than 0% verified.
#     Every rate here therefore travels with its denominator.
#   * **An empty bucket is present and zeroed, but its percentiles are
#     `None`.** A chart that coerces those to `0` draws a cliff to the axis on
#     every quiet hour.


class Percentiles(BaseModel):
    """Nearest-rank p50 and p95. `None` when nothing was measured.

    At n=1 both are the one value and at n=2 p50 is the lower: small windows
    showing `p50 == p95` are correct, not a bug.
    """

    p50: float | None = None
    p95: float | None = None


class LatencyPercentiles(Percentiles):
    #: Reported beside the percentiles as context and never on its own: the
    #: tail is what breaks a demo and the mean hides it.
    mean: float | None = None


class LiveCounts(BaseModel):
    """Workers busy and runs queued, from `JobRunner` -- not from a table.

    Merged in by the route, because a table read would be describing the last
    request rather than this process.
    """

    running: int = 0
    queued: int = 0
    active: int = 0


class RunCounts(BaseModel):
    total: int = 0
    #: The denominator of every reliability rate. A queued run has not failed,
    #: and dividing by `total` would make the failure rate fall every time
    #: somebody submitted work.
    settled: int = 0
    done: int = 0
    failed: int = 0
    interrupted: int = 0
    cancelled: int = 0
    live: int = 0
    criteria: int = 0


class Reliability(BaseModel):
    """`failed + interrupted` over `settled`. Three outcomes, not two:
    done-but-`needs_review` is quality and is on its own meter."""

    failure_rate: float | None = None
    failed: int = 0
    interrupted: int = 0


class CostSummary(BaseModel):
    total: float = 0.0
    mean: float | None = None
    p50: float | None = None
    p95: float | None = None


class TokenCounts(BaseModel):
    input: int = 0
    output: int = 0
    tool_calls: int = 0


class EvaluatorSlot(BaseModel):
    """The accept-rate meter, honestly empty.

    `analyses.evaluator_*` are declared and `NULL` until the evaluator lands,
    so the slot reports what it is actually showing. A UI must label `value`
    by `showing` and never as an accept rate while `available` is false.
    """

    available: bool = False
    accept_rate: float | None = None
    showing: str = "cap_rate"
    value: float | None = None
    note: str = ""


class Quality(BaseModel):
    """The three meters, each with the denominator it was computed over."""

    quote_verification_rate: float | None = None
    quotes_total: int = 0
    quotes_verified: int = 0
    needs_review_rate: float | None = None
    needs_review: int = 0
    runs_needing_review: int = 0
    mean_confidence: float | None = None
    cap_rate: float | None = None
    capped: int = 0
    runs_capped: int = 0
    evaluator: EvaluatorSlot


class SurfaceCost(BaseModel):
    surface: str | None = None
    runs: int = 0
    cost_usd: float = 0.0


class ModelCost(BaseModel):
    """From `agent.call` spans, so it covers analysis and chat in one pass.
    Empty for a window with no calls, or a database whose spans predate the
    table; token counts are best-effort."""

    model: str | None = None
    calls: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


class ChatSummary(BaseModel):
    """Chat is stateless and writes no run row, so this is `spans WHERE name =
    'chat'`. **`latency_ms` is milliseconds** while `latency_s` in the same
    payload is seconds."""

    turns: int = 0
    cost_usd: float = 0.0
    cost_per_turn: float | None = None
    errors: int = 0
    latency_ms: Percentiles


class SpanCounts(BaseModel):
    """Spans recorded and spans thrown away. Reported because a metrics system
    that silently loses data is worse than one that says it lost some."""

    written: int = 0
    dropped: int = 0


class MetricsSummary(BaseModel):
    """`GET /metrics/summary`: every tile and meter on the KPI page."""

    window: str
    #: The lower bound of the window, spelled the way `created_at` is.
    since: str
    generated_at: str
    live: LiveCounts
    runs: RunCounts
    reliability: Reliability
    #: Seconds. See `chat.latency_ms`, which is not.
    latency_s: LatencyPercentiles
    cost_usd: CostSummary
    tokens: TokenCounts
    quality: Quality
    surfaces: list[SurfaceCost]
    cost_by_model: list[ModelCost]
    chat: ChatSummary
    documents: int = 0
    spans: SpanCounts


class BucketChat(BaseModel):
    turns: int = 0
    cost_usd: float = 0.0


class MetricsBucket(BaseModel):
    """One point on the trend charts.

    **The last bucket in a series is the current one and is partial**, so its
    bar is always short: mark it or drop it, but do not let a reader read the
    present as a fall.
    """

    #: Epoch-aligned, not now-aligned, so the same run lands in the same bucket
    #: however often the page refreshes.
    bucket: str
    runs: int = 0
    done: int = 0
    failed: int = 0
    cost_usd: float = 0.0
    latency_s: Percentiles
    cost_percentiles: Percentiles
    mean_confidence: float | None = None
    quote_verification_rate: float | None = None
    needs_review_rate: float | None = None
    #: Compliance states to their counts, mined out of the stored reports.
    states: dict[str, int]
    chat: BucketChat


class RunRow(BaseModel):
    """One row of the global runs table.

    `report_json` is deliberately absent -- a runs table wants none of thirty
    kilobytes of report per row. **`trace_id` is why this table exists**: it is
    the join from a number on the KPI page to the lines in `.run/app.jsonl`
    that produced it.
    """

    analysis_id: str
    trace_id: str | None = None
    document_id: int
    filename: str = ""
    surface: str = "api"
    status: str = "queued"
    criteria_requested: int = 0
    criteria_completed: int = 0
    criteria_skipped: int = 0
    error: str | None = None
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    #: Seconds.
    latency_s: float | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    tool_calls: int | None = None
    needs_review: int | None = None
    capped: int | None = None
    mean_confidence: float | None = None
    quotes_total: int | None = None
    quotes_verified: int | None = None


class SpanNode(BaseModel):
    """One span, carrying the spans it opened.

    The tree is resolved server-side so the same `parent_span_id` algorithm is
    not written again in TypeScript. A span whose parent is missing is promoted
    to a root rather than dropped, so a run can have more roots than the shape
    `api.analysis -> analysis.document -> ...` suggests.
    """

    span_id: str
    parent_span_id: str | None = None
    trace_id: str | None = None
    run_id: str | None = None
    name: str
    status: str | None = None
    #: Milliseconds, unlike `latency_s` everywhere else on this page.
    latency_ms: float | None = None
    ts: str
    surface: str | None = None
    criterion: str | None = None
    document_id: int | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    #: Whatever else the span bag held. `{}` when it could not be parsed -- a
    #: span with an unreadable bag should still show its timing.
    attrs: dict[str, Any]
    children: list[SpanNode]


class StageBucket(BaseModel):
    """One bucket: worst-stage error rate, and error count across all named stages.

    Rates and totals are null when that bucket had no spans, not zero.
    """

    bucket: str
    n: int = 0
    error_rate: float | None = None
    errors_total: int | None = None


class MonitorStages(BaseModel):
    """`GET /monitor/stages`: the worst pipeline stage, and its trend.

    Tiles are the last five minutes when anything ran; otherwise the chart
    window. `n` under `min_samples` is not a rate worth paging on.
    `errors_total` is every named stage in that tile window, not just `name`.
    """

    window: str
    live_window: str
    since: str
    generated_at: str
    name: str | None = None
    n: int = 0
    errors: int = 0
    error_rate: float | None = None
    errors_total: int | None = None
    min_samples: int
    series: list[StageBucket]


class HostBucket(BaseModel):
    """One chart bucket. Percents are null when nothing was sampled then."""

    bucket: str
    rss_pct: float | None = None
    disk_used_pct: float | None = None


class MonitorHost(BaseModel):
    """`GET /monitor/host`: latest RAM and disk, and their trend.

    Tiles are the newest `system_samples` row. Charts use one bar per
    `monitor_sample_seconds` tick. HTTP columns on that table are not part of
    this payload.
    """

    window: str
    bucket: str
    since: str
    generated_at: str
    ts: str | None = None
    rss_mb: float | None = None
    rss_pct: float | None = None
    disk_used_pct: float | None = None
    disk_used_gb: float | None = None
    disk_total_gb: float | None = None
    series: list[HostBucket]


class UpstreamBucket(BaseModel):
    """One minute of outbound calls. Rates are null when no call landed."""

    bucket: str
    calls: int = 0
    retries: int = 0
    failed: int = 0
    retries_per_100: float | None = None
    exhausted_rate: float | None = None


class MonitorUpstream(BaseModel):
    """`GET /monitor/upstream`: retries through http_client, not spend.

    Tiles are the last five minutes when anything was called; otherwise the
    chart window. `top_reason_share` is that reason's share of retry +
    exhausted events, not of all calls.
    """

    window: str
    live_window: str
    since: str
    generated_at: str
    calls: int = 0
    retries: int = 0
    failed: int = 0
    retries_per_100: float | None = None
    exhausted_rate: float | None = None
    top_reason: str | None = None
    top_reason_share: float | None = None
    series: list[UpstreamBucket]


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
# `children: list[SpanNode]` is a forward reference to the class being
# defined; without this the OpenAPI document would be exported from an
# unresolved model.
SpanNode.model_rebuild()

__all__ = [
    "Analysis",
    "AnalysisSummary",
    "AnalyzeRequest",
    "Answer",
    "BucketChat",
    "ChatRequest",
    "ChatSummary",
    "CitationOut",
    "CostSummary",
    "CriterionOut",
    "CriterionProgress",
    "Detail",
    "DocumentDetail",
    "DocumentOut",
    "Error",
    "ErrorBody",
    "EvaluatorSlot",
    "Health",
    "JobStatus",
    "LastAnalysisOut",
    "LatencyPercentiles",
    "LiveCounts",
    "Message",
    "MetricsBucket",
    "MetricsSummary",
    "ModelCost",
    "PassageOut",
    "Percentiles",
    "Progress",
    "Quality",
    "Reliability",
    "RunCounts",
    "RunRow",
    "SearchOut",
    "SearchRequest",
    "SectionOut",
    "SpanCounts",
    "SpanNode",
    "SurfaceCost",
    "TokenCounts",
    "UploadOut",
    "answer_of",
    "as_dict",
    "summarise_report",
]
