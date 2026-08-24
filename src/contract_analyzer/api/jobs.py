"""Background analyses: submit, poll, stream, cancel.

A five-criterion run is a minute at best and three minutes at worst -- past any
browser, proxy or MCP client timeout -- so `POST /analyses` hands back an id and
the client comes back for the answer. The id is the whole of the state: this
server keeps nothing per client, which is what lets the UI, an MCP tool call and
a connector all watch the same job.

**What is remembered, and for how long.** Two halves. The `analyses` row is the
durable one: it survives a restart, it is what another worker can read, and it
carries the report. `JobState` in a dict on the app is the live one: `stage`,
per-criterion progress, the SSE subscribers, the cancel flag -- none of which
mean anything once the process holding them is gone. **The dict wins wherever
both have an answer**, because it is the one being updated; the row answers
everything else, which after a restart is everything.

Durable is not distributed. With `--workers 2` a worker can read a neighbour's
analysis and its report, and it still cannot stream its events or cancel it:
`Broadcast` and the cancel `threading.Event` are per-process objects, and
`find_live` stays in-memory for the same reason -- this process cannot hand
back a live handle it does not own. Fixing that means a broker, which is what
this iteration declined to build for a local demo.

**Two jobs at a time, and the third waits.** SQLite serialises writes and the
answer model has rate limits; `api_workers * analysis_workers` is the real
ceiling on concurrent requests, and a bounded pool is the only rate limit a
local demo needs. A third submission is `queued` and the client sees it.

**A duplicate submission is not a second run.** At roughly a dollar a run, a
double-clicked button is a real cost, so a submission matching a job already
queued or running comes back as *that* job. `Idempotency-Key` overrides the
match for a caller that genuinely wants a second opinion.

**Cancel is checked between criteria.** `JobRunner.cancel` sets a flag the
runner polls before each criterion starts, and cancels the future outright if
the pool has not picked it up. A criterion already talking to the model finishes
-- stopping it would mean threading the flag into the agent loop, which belongs
to `generation/`, not here. The report that comes back is partial and says so.
"""

from __future__ import annotations

import contextvars
import sqlite3
import threading
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .. import analyses as store
from ..analyses import AnalysisRecord
from ..compliance.criteria import get_criteria
from ..config import Settings
from ..db import get_db
from ..embeddings.base import Embedder
from ..logger import get_logger, new_id, run_context, span, trace_context
from ..report import AnalysisReport, AnalysisTotals, analyze_document, totals_of
from .schemas import Analysis, AnalysisSummary, CriterionProgress, JobStatus, Progress
from .sse import Broadcast

log = get_logger(__name__)

#: Statuses a submission can join instead of starting a second run.
_LIVE: frozenset[str] = frozenset({"queued", "running"})


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class JobState:
    """One analysis: what it is, how far it has got, and who is watching."""

    analysis_id: str
    document_id: int
    criteria: tuple[str, ...]
    trace_id: str
    events: Broadcast
    status: JobStatus = "queued"
    stage: str = "queued"
    report: AnalysisReport | None = None
    error: str | None = None
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    completed_at: str | None = None
    #: Set by `cancel`, read by the runner between criteria.
    cancelled: threading.Event = field(default_factory=threading.Event)
    future: Future | None = None
    #: Per criterion, in the order they will be reported.
    progress: dict[str, CriterionProgress] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def key(self) -> tuple[int, tuple[str, ...]]:
        """What makes two submissions the same submission."""
        return self.document_id, self.criteria

    @property
    def done(self) -> int:
        return sum(1 for p in self.progress.values() if p.status in ("done", "skipped"))

    def summary(self) -> AnalysisSummary:
        with self._lock:
            return AnalysisSummary(
                analysis_id=self.analysis_id,
                document_id=self.document_id,
                status=self.status,
                stage=self.stage,
                progress=Progress(done=self.done, total=len(self.progress)),
                criteria=[p.model_copy() for p in self.progress.values()],
                totals=self.report.totals if self.report else None,
                trace_id=self.trace_id,
                error=self.error,
                created_at=self.created_at,
                started_at=self.started_at,
                completed_at=self.completed_at,
            )

    def detail(self, report: AnalysisReport | None) -> Analysis:
        return Analysis(**self.summary().model_dump(), report=report)

    # -- transitions, each of which is also an event ----------------------

    def running(self) -> None:
        with self._lock:
            self.status = "running"
            self.started_at = _now()
            self.stage = f"criterion 0/{len(self.progress)}"
        self.events.publish("status", _status_payload(self))

    def criterion_done(self, criterion_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            entry = self.progress.get(criterion_id)
            if entry is not None:
                entry.status = "done"
                entry.state = event.get("state")
                entry.confidence = event.get("confidence")
                entry.needs_review = event.get("needs_review")
                entry.latency_s = event.get("latency_s")
            self.stage = f"criterion {self.done}/{len(self.progress)}"
        self.events.publish("criterion", {"criterion": criterion_id, **_progress_payload(self)})

    def criterion_started(self, criterion_id: str) -> None:
        """The first event tagged with this criterion means it is under way.

        There is no `started` event to listen for, and there does not need to
        be: a criterion's first act is always a search, so its first `tool_call`
        is the signal. Idempotent, because there are several of them.

        **Nothing is published.** The stream's contract is one `criterion` event
        per *verdict*, and a second one meaning "started" would make every
        subscriber count them to tell the two apart. A poller sees the change on
        its next tick, which is how the UI reads this table anyway, and an SSE
        subscriber already sees the `tool_call` this was inferred from.
        """
        with self._lock:
            entry = self.progress.get(criterion_id)
            if entry is None or entry.status != "queued":
                return
            entry.status = "running"

    def criterion_skipped(self, criterion_id: str) -> None:
        with self._lock:
            entry = self.progress.get(criterion_id)
            if entry is not None:
                entry.status = "skipped"
        self.events.publish("criterion", {"criterion": criterion_id, "status": "skipped"})

    def finish(self, report: AnalysisReport) -> None:
        with self._lock:
            self.report = report
            self.status = "cancelled" if report.status == "cancelled" else "done"
            self.stage = self.status
            self.completed_at = _now()
        self.events.close("done", _status_payload(self))

    def fail(self, error: str) -> None:
        with self._lock:
            self.status = "failed"
            self.stage = "failed"
            self.error = error
            self.completed_at = _now()
        self.events.close("error", {"analysis_id": self.analysis_id, "error": error})


# -- the other producer of AnalysisSummary: a row ---------------------------


def summary_of(record: AnalysisRecord, report: AnalysisReport | None = None) -> AnalysisSummary:
    """An `analyses` row as the wire type, for an analysis this process is not
    running -- one from a previous boot, or from the CLI, or from a neighbour.

    Here, beside `JobState.summary()`, so the two producers of `AnalysisSummary`
    sit together and neither drifts from the other.

    A row carries no `stage`, no live `progress` and no per-criterion list. The
    missing fields are filled with **the terminal values the status implies**
    rather than invented ones: `stage` is the status, and `progress` counts what
    the row says finished.

    `totals` is rebuilt from the row's own columns rather than from the stored
    report -- they were derived from it and they are the same numbers, and a
    list endpoint should not parse thirty kilobytes of JSON per entry to fill
    them in. `criteria` is the one field that genuinely needs the report, so it
    is filled only when a caller has already parsed one; `detail_of` does.
    """
    return AnalysisSummary(
        analysis_id=record.analysis_id,
        document_id=record.document_id,
        status=record.status,  # type: ignore[arg-type]
        stage=record.status,
        progress=Progress(
            done=record.criteria_completed + record.criteria_skipped,
            total=record.criteria_requested,
        ),
        criteria=_criteria_of(report),
        totals=_totals_of(record),
        trace_id=record.trace_id,
        error=record.error,
        created_at=record.created_at,
        started_at=record.started_at,
        completed_at=record.completed_at,
    )


def detail_of(record: AnalysisRecord, report: AnalysisReport | None = None) -> Analysis:
    """The same, with the report. `report` is passed in when the caller has
    already trimmed it for `?detail=summary`; otherwise it is read from the
    row."""
    if report is None:
        report = record.report()
    return Analysis(**summary_of(record, report).model_dump(), report=report)


def summaries_for(
    runner: JobRunner, conn: sqlite3.Connection, document_id: int, *, limit: int = 50
) -> list[AnalysisSummary]:
    """One document's analyses: the live ones and the stored ones, merged.

    The dict wins on the analyses it holds -- it has the stage and the live
    progress the row cannot carry -- and the row supplies everything else,
    which after a restart is everything. Used by `GET /analyses` and by
    `GET /documents/{id}`, so the two cannot disagree.
    """
    live = {job.analysis_id: job.summary() for job in runner.list(document_id)}
    stored = [
        summary_of(record)
        for record in store.list_analyses(conn, document_id, limit=limit)
        if record.analysis_id not in live
    ]
    merged = list(live.values()) + stored
    return sorted(merged, key=lambda s: (s.created_at, s.analysis_id), reverse=True)


def _totals_of(record: AnalysisRecord) -> AnalysisTotals | None:
    """The derived columns as `AnalysisTotals`, or None for a run that never
    produced a report. `report_json` is the flag, so this costs no parse."""
    if not record.report_json:
        return None
    return AnalysisTotals(
        criteria=record.criteria_completed,
        latency_s=record.latency_s or 0.0,
        cost_usd=record.cost_usd or 0.0,
        input_tokens=record.input_tokens or 0,
        output_tokens=record.output_tokens or 0,
        tool_calls=record.tool_calls or 0,
        needs_review=record.needs_review or 0,
        capped=record.capped or 0,
        mean_confidence=record.mean_confidence or 0.0,
    )


def _criteria_of(report: AnalysisReport | None) -> list[CriterionProgress]:
    """The progress table as a finished report implies it. Nothing is invented:
    a criterion is `done` because there is a result for it, or `skipped`
    because the report says the run never reached it."""
    if report is None:
        return []
    return [
        CriterionProgress(
            id=result.criterion_id,
            status="done",
            state=result.compliance_state,
            confidence=result.confidence,
            needs_review=result.needs_review,
            latency_s=result.latency_s,
        )
        for result in report.results
    ] + [CriterionProgress(id=criterion_id, status="skipped") for criterion_id in report.skipped]


def _status_payload(job: JobState) -> dict[str, Any]:
    return {
        "analysis_id": job.analysis_id,
        "document_id": job.document_id,
        "status": job.status,
        "stage": job.stage,
        **_progress_payload(job),
    }


def _progress_payload(job: JobState) -> dict[str, Any]:
    return {"done": job.done, "total": len(job.progress)}


class JobRunner:
    """The pool, the job table, and the worker. One instance per app."""

    def __init__(
        self,
        settings: Settings,
        embedder: Embedder | None = None,
        client: Any = None,
    ) -> None:
        self._settings = settings
        self._embedder = embedder
        self._client = client
        self._pool = ThreadPoolExecutor(
            max_workers=settings.api_workers, thread_name_prefix="analysis"
        )
        self._jobs: dict[str, JobState] = {}
        self._lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------

    def shutdown(self) -> None:
        """Stop taking work and let what is running finish. Called by lifespan.

        Jobs still queued are cancelled: nothing will pick them up, and a state
        of `queued` on a dead runner is a lie a client would poll forever.

        Their rows are left alone rather than failed here. The next startup's
        `reconcile` reads them as `interrupted`, which is what actually
        happened -- the process went away -- and is a better answer than
        `failed` from a runner that is itself being torn down.
        """
        with self._lock:
            for job in self._jobs.values():
                if job.status == "queued":
                    job.cancelled.set()
                    job.fail("the server shut down before this analysis started")
        self._pool.shutdown(wait=True, cancel_futures=True)

    # -- queries ----------------------------------------------------------

    def get(self, analysis_id: str) -> JobState | None:
        with self._lock:
            return self._jobs.get(analysis_id)

    def list(self, document_id: int) -> list[JobState]:
        """One document's analyses, newest first. Scoped on purpose: a global
        list is a KPI question, and `/metrics/runs` is where it belongs."""
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.document_id == document_id]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    @property
    def active(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.status in _LIVE)

    @property
    def live(self) -> tuple[int, int]:
        """Running and queued, counted in one pass under one lock.

        The KPI page's "Active now" tile is `workers busy - queued`, and the
        two halves have to agree: reading `active` and then a second property
        would let a job start between them and show a queued run that is no
        longer queued. Not stored anywhere -- a queue depth is a fact about
        this process, and a table would be describing the last one.
        """
        with self._lock:
            statuses = [j.status for j in self._jobs.values()]
        return (
            sum(1 for s in statuses if s == "running"),
            sum(1 for s in statuses if s == "queued"),
        )

    def find_live(self, document_id: int, criteria: tuple[str, ...]) -> JobState | None:
        """A job already doing exactly this, if there is one."""
        with self._lock:
            for job in self._jobs.values():
                if job.status in _LIVE and job.key == (document_id, criteria):
                    return job
        return None

    # -- commands ---------------------------------------------------------

    def submit(
        self,
        conn: sqlite3.Connection,
        document_id: int,
        criteria: Sequence[str] | None,
        *,
        trace_id: str,
        filename: str = "",
        surface: str = "api",
    ) -> JobState:
        """Queue an analysis and return its state. Never blocks.

        `conn` is the *request's* connection, passed in rather than opened here.
        `queue_analysis` is one INSERT on the request thread, and the worker's
        connection does not exist yet at this point; opening a second one for
        it would be a connection per submission for no reason.

        This is the only lifecycle write the API makes. `running`, `done`,
        `cancelled` and `failed` are all written by `analyze_document`, so the
        CLI records them too -- `queued` is the one state HTTP has and the
        command line does not.
        """
        selected = self.resolve_criteria(criteria)
        job = JobState(
            analysis_id=new_id(16),
            document_id=document_id,
            criteria=selected,
            trace_id=trace_id,
            events=Broadcast(self._settings.api_event_buffer),
            progress={cid: CriterionProgress(id=cid) for cid in selected},
        )
        store.queue_analysis(
            conn,
            job.analysis_id,
            document_id,
            filename=filename,
            criteria=selected,
            trace_id=trace_id,
            surface=surface,
        )
        with self._lock:
            self._jobs[job.analysis_id] = job
        job.future = self._pool.submit(contextvars.copy_context().run, self._run, job)
        log.info(
            "api.analysis.queued",
            extra={
                "analysis_id": job.analysis_id,
                "document_id": document_id,
                "criteria": len(selected),
            },
        )
        return job

    def cancel(self, conn: sqlite3.Connection, job: JobState) -> None:
        """Ask the run to stop after the criteria that have already started."""
        job.cancelled.set()
        if job.future is not None and job.future.cancel():
            # Never picked up by the pool: `analyze_document` will not run, so
            # nothing else will ever set the terminal state -- in memory or on
            # disk. Both are set here, on the request's connection.
            report = AnalysisReport(
                analysis_id=job.analysis_id,
                document_id=job.document_id,
                status="cancelled",
                trace_id=job.trace_id,
                skipped=list(job.criteria),
                totals=totals_of([]),
                created_at=job.created_at,
                completed_at=_now(),
            )
            job.finish(report)
            store.finish_analysis(conn, job.analysis_id, report)
        log.info("api.analysis.cancelled", extra={"analysis_id": job.analysis_id})

    # -- the worker -------------------------------------------------------

    def _run(self, job: JobState) -> None:
        """One analysis, on a pool thread, under the trace of the request that
        asked for it. Every exit path ends the job and closes its stream."""
        # `run_context` here as well as inside `analyze_document`, so that the
        # span covering the whole job -- queued through finished -- belongs to
        # the run too. Without it the waterfall's root would be
        # `analysis.document` and the time the job spent waiting for a worker
        # would be missing from the one view that should show it.
        with trace_context(job.trace_id), run_context(job.analysis_id), span(
            "api.analysis", log, analysis_id=job.analysis_id, document_id=job.document_id
        ):
            job.running()
            conn = get_db(self._settings, same_thread=False)
            try:
                report = analyze_document(
                    job.document_id,
                    conn,
                    self._embedder,
                    self._settings,
                    self._client,
                    criteria=list(job.criteria),
                    on_event=lambda event: self._on_event(job, event),
                    cancelled=job.cancelled.is_set,
                    analysis_id=job.analysis_id,
                    surface="api",
                )
            except BaseException as exc:  # noqa: BLE001 - a job must never die silently
                log.exception("api.analysis.failed", extra={"analysis_id": job.analysis_id})
                job.fail(f"{type(exc).__name__}: {exc}")
                # Belt and braces. `analyze_document` fails its own row, but it
                # raises the two argument errors -- unknown document, unknown
                # criterion -- *before* there is one to fail, and this row was
                # already queued. Without this it would sit at `queued` until
                # the next restart reconciled it.
                store.fail_analysis(conn, job.analysis_id, f"{type(exc).__name__}: {exc}")
            else:
                job.finish(report)
            finally:
                conn.close()

    def _on_event(self, job: JobState, event: dict[str, Any]) -> None:
        """The runner calls this serially, so nothing here needs a lock of its
        own beyond what `JobState` already holds."""
        kind = event.get("type")
        criterion = event.get("criterion", "")
        if criterion and kind not in ("result", "skipped"):
            job.criterion_started(criterion)
        if kind == "result":
            job.criterion_done(criterion, event)
        elif kind == "skipped":
            job.criterion_skipped(criterion)
        elif kind == "tool_call":
            job.events.publish(
                "tool_call",
                {
                    "criterion": criterion,
                    "name": event.get("name"),
                    "args": event.get("args"),
                    "returned": event.get("returned"),
                    "new": event.get("new"),
                    "error": event.get("error"),
                },
            )
        elif kind == "structure_errors":
            job.events.publish(
                "correction",
                {
                    "criterion": criterion,
                    "round": event.get("round"),
                    "errors": event.get("errors"),
                },
            )

    def resolve_criteria(self, criteria: Sequence[str] | None) -> tuple[str, ...]:
        """Criterion ids in their defined order, whatever order was asked for.

        Validated here rather than on the worker: an unknown id is the client's
        mistake and deserves a 422 now, not a failed job in thirty seconds.
        """
        everything = [c.id for c in get_criteria()]
        if not criteria:
            return tuple(everything)
        unknown = sorted(set(criteria) - set(everything))
        if unknown:
            raise KeyError(", ".join(unknown))
        wanted = set(criteria)
        return tuple(cid for cid in everything if cid in wanted)


__all__ = ["JobRunner", "JobState", "detail_of", "summaries_for", "summary_of"]
