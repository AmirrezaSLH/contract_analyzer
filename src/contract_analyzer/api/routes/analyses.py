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
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response, status
from sse_starlette.sse import EventSourceResponse

from ...documents import get_document
from ...logger import current_trace_id, get_logger
from ..deps import ClientDep, ConnDep, Protected, RunnerDep, SettingsDep
from ..errors import ApiError, analysis_not_found, document_not_found, no_api_key
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
        "Returns immediately with an `analysis_id`; poll `GET /analyses/{id}` or subscribe "
        "to `GET /analyses/{id}/events`. A submission that matches one already queued or "
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
    if get_document(conn, body.document_id) is None:
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
            "Call GET /criteria for the ids this service knows.",
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

    job = runner.submit(body.document_id, criteria, trace_id=current_trace_id() or "")
    return job.summary()


@router.get(
    "",
    summary="One document's analyses, newest first",
    description="`document_id` is required: a global list is a KPI question, and "
                "`/metrics/runs` is where it belongs.",
)
def index(runner: RunnerDep, document_id: Annotated[int, Query()]) -> list[AnalysisSummary]:
    return [job.summary() for job in runner.list(document_id)]


@router.get(
    "/{analysis_id}",
    summary="An analysis, with its report once it is done",
    description="`detail=summary` omits quotes and rationale -- the MCP default, because a "
                "full report is a lot of context to put in a model's window.",
)
def detail(
    analysis_id: str,
    runner: RunnerDep,
    detail: Annotated[Detail, Query()] = "full",
) -> Analysis:
    job = runner.get(analysis_id)
    if job is None:
        raise analysis_not_found(analysis_id)
    report = job.report
    if report is not None and detail == "summary":
        report = summarise_report(report)
    return job.detail(report)


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
def cancel(analysis_id: str, runner: RunnerDep) -> AnalysisSummary:
    job = runner.get(analysis_id)
    if job is None:
        raise analysis_not_found(analysis_id)
    if job.status not in ("queued", "running"):
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "not_running",
            f"Analysis {analysis_id} is already {job.status}.",
        )
    runner.cancel(job)
    return job.summary()


@router.get(
    "/{analysis_id}/events",
    summary="Progress as server-sent events",
    description=(
        "`status`, one `criterion` per verdict, `tool_call` for each search the agents "
        "make, then `done` or `error`, after which the stream closes. Subscribing late "
        "replays what was missed; subscribing after the job finished returns the replay "
        "and closes, rather than hanging."
    ),
    response_class=EventSourceResponse,
)
def events(analysis_id: str, request: Request, runner: RunnerDep, settings: SettingsDep):
    job = runner.get(analysis_id)
    if job is None:
        raise analysis_not_found(analysis_id)

    def stream():
        for event in job.events.subscribe():
            yield {"event": event.name, "data": event.json}

    return EventSourceResponse(
        stream(),
        ping=int(settings.api_keepalive_seconds),
        headers={"X-Trace-Id": job.trace_id} if job.trace_id else None,
    )
