"""Analyses as jobs: submit, poll, stream, cancel.

`POST /analyses` returns in well under a second with an id; the analysis itself
takes a minute at best. The id is the state handle, and polling `GET
/analyses/{id}` is the contract -- `/events` is an upgrade for a client that
wants the criteria as they land, not a requirement.

Two things are decided in this module rather than on the worker, because both
are the caller's mistake and deserve an answer now rather than a failed job in
thirty seconds: an unknown `document_id` or criterion id, and a missing answer
key. `get_client` raising `AnswerUnavailable` on the pool thread would mean a
`202` followed by a job that fails immediately, which is a worse answer than an
error.

**Reads are the live job first, the stored row second.** An analysis running in
this process has a stage, a progress table and a stream; one from a previous
boot, from `make analyze` or from another worker has a row and a report. The
first two operations here need the live handle and say so when they do not have
it -- `cancel` cannot set a flag it does not own, `events` cannot fan out a
stream that lives in another process -- while `GET` answers from either.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response, status
from sse_starlette.sse import EventSourceResponse

from ...analyses import get_analysis
from ...db import get_db
from ...documents import get_document
from ...logger import current_trace_id, get_logger
from ..deps import ClientDep, ConnDep, Protected, RunnerDep, SettingsDep
from ..errors import ApiError, analysis_not_found, document_not_found, no_api_key
from ..jobs import detail_of, summaries_for
from ..schemas import (
    Analysis,
    AnalysisSummary,
    AnalyzeRequest,
    Detail,
    summarise_report,
)

log = get_logger(__name__)

router = APIRouter(prefix="/analyses", tags=["analyses"], dependencies=[Protected])


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue an analysis of one contract",
    description=(
        "Returns immediately with an `analysis_id`; poll `GET /api/analyses/{id}` or "
        "subscribe to `GET /api/analyses/{id}/events`. A submission that matches one "
        "already queued or "
        "running returns that analysis with `200` instead of starting a second run -- send "
        "an `Idempotency-Key` header to force a new one."
    ),
)
def submit(
    body: AnalyzeRequest,
    response: Response,
    conn: ConnDep,
    runner: RunnerDep,
    client: ClientDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AnalysisSummary:
    document = get_document(conn, body.document_id)
    if document is None:
        raise document_not_found(body.document_id)
    if client is None:
        raise no_api_key()

    try:
        criteria = runner.resolve_criteria(body.criteria)
    except KeyError as exc:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "validation",
            f"Unknown criterion id(s): {exc.args[0]}.",
            "Use one of the five criterion ids this service publishes, or omit the "
            "field to run all of them.",
        ) from None

    if idempotency_key is None:
        existing = runner.find_live(body.document_id, criteria)
        if existing is not None:
            # 200, not 202: nothing was accepted, an existing job was returned.
            response.status_code = status.HTTP_200_OK
            log.info(
                "api.analysis.duplicate",
                extra={"analysis_id": existing.analysis_id, "document_id": body.document_id},
            )
            return existing.summary()

    # The request's connection, not one of the runner's: `queue_analysis` is a
    # single INSERT on this thread, and the worker's connection does not exist
    # yet. `filename` is denormalised onto the row so a report outlives the
    # document it was run against.
    job = runner.submit(
        conn,
        body.document_id,
        criteria,
        trace_id=current_trace_id() or "",
        filename=document.filename,
    )
    return job.summary()


@router.get(
    "",
    summary="One document's analyses, newest first",
    description="Live and stored analyses merged, so a run from a previous boot or from "
                "`make analyze` is listed too. `document_id` is required: a global list is "
                "a KPI question, and `/metrics/runs` is where it belongs.",
)
def index(
    conn: ConnDep, runner: RunnerDep, document_id: Annotated[int, Query()]
) -> list[AnalysisSummary]:
    return summaries_for(runner, conn, document_id)


@router.get(
    "/{analysis_id}",
    summary="An analysis, with its report once it is done",
    description="Answered from the live job when this process is running it, and from the "
                "stored record otherwise -- a finished analysis returns its report after a "
                "restart. `detail=summary` omits quotes and rationale -- the MCP default, "
                "because a full report is a lot of context to put in a model's window.",
)
def detail(
    analysis_id: str,
    conn: ConnDep,
    runner: RunnerDep,
    detail: Annotated[Detail, Query()] = "full",
) -> Analysis:
    job = runner.get(analysis_id)
    if job is not None:
        return job.detail(_trim(job.report, detail))
    record = get_analysis(conn, analysis_id)
    if record is None:
        raise analysis_not_found(analysis_id)
    return detail_of(record, _trim(record.report(), detail))


@router.post(
    "/{analysis_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Stop an analysis after the criteria already in flight",
    description=(
        "Criteria that have not started are skipped and listed in the report's `skipped`. "
        "One already talking to the model finishes: stopping it would mean interrupting "
        "the agent loop between tool calls, which this API does not do."
    ),
)
def cancel(analysis_id: str, conn: ConnDep, runner: RunnerDep) -> AnalysisSummary:
    job = runner.get(analysis_id)
    if job is None:
        raise _not_here(get_analysis(conn, analysis_id) is not None, analysis_id, "cancelled")
    if job.status not in ("queued", "running"):
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "not_running",
            f"Analysis {analysis_id} is already {job.status}.",
        )
    runner.cancel(conn, job)
    return job.summary()


@router.get(
    "/{analysis_id}/events",
    summary="Progress as server-sent events",
    description=(
        "`status`, one `criterion` per verdict, `evaluating` and `revising` as the "
        "critic reads a draft and the Router sends one back, `decision` for what the "
        "Router concluded, `tool_call` for each search the agents "
        "make, then `done` or `error`, after which the stream closes. Subscribing late "
        "replays what was missed; subscribing after the job finished returns the replay "
        "and closes, rather than hanging."
    ),
    response_class=EventSourceResponse,
)
def events(analysis_id: str, request: Request, runner: RunnerDep, settings: SettingsDep):
    job = runner.get(analysis_id)
    if job is None:
        # No `ConnDep` on this route: `deps.py` is explicit that a streaming
        # response must not hold a per-request connection, and an SSE stream is
        # open for the length of the analysis. The one lookup this path needs
        # gets its own connection and closes it here.
        conn = get_db(settings)
        try:
            raise _not_here(get_analysis(conn, analysis_id) is not None, analysis_id, "streamed")
        finally:
            conn.close()

    def stream():
        for event in job.events.subscribe():
            yield {"event": event.name, "data": event.json}

    return EventSourceResponse(
        stream(),
        ping=int(settings.api_keepalive_seconds),
        headers={"X-Trace-Id": job.trace_id} if job.trace_id else None,
    )


def _trim(report, detail: Detail):
    """`?detail=summary` without the quotes and the rationale. A copy: the
    runner's report is the one every other reader of this job holds."""
    return summarise_report(report) if report is not None and detail == "summary" else report


def _not_here(stored: bool, analysis_id: str, verb: str) -> ApiError:
    """404 for an id nobody has, 409 for one this process does not own.

    Cancelling and streaming both need the live handle -- a `threading.Event`
    and a `Broadcast`, neither of which crosses a process. An analysis that
    exists on disk but not in this worker's dict is therefore a conflict, not a
    missing resource, and saying so is better than a 404 that contradicts the
    `GET` the client just made.
    """
    if not stored:
        return analysis_not_found(analysis_id)
    return ApiError(
        status.HTTP_409_CONFLICT,
        "not_live_here",
        f"Analysis {analysis_id} is not running in this process, so it cannot be {verb}.",
        f"Ask for its status instead -- GET /api/analyses/{analysis_id} answers from the "
        "stored record, wherever the run is. Live events and cancellation reach only "
        "the worker that started it.",
    )
