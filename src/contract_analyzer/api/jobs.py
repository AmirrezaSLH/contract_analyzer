"""Background analyses: submit, poll, stream, cancel.

A five-criterion run is a minute at best and three minutes at worst -- past any
browser, proxy or MCP client timeout -- so `POST /analyses` hands back an id and
the client comes back for the answer. The id is the whole of the state: this
server keeps nothing per client, which is what lets the UI, an MCP tool call and
a connector all watch the same job.

**What is remembered, and for how long.** `JobState` lives in a dict on the app.
That is the honest scope of this iteration: an analysis does not survive a
restart, and `GET /analyses/{id}` says so in its 404 hint. When the metrics
store lands, the `runs` row becomes the durable half and this dict stays as the
live view (stage, progress, subscribers, the cancel flag) -- which is why it is
built around an interface rather than around the dict.

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
import threading
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..compliance.criteria import get_criteria
from ..config import Settings
from ..db import get_db
from ..embeddings.base import Embedder
from ..logger import get_logger, new_id, span, trace_context
from ..report import AnalysisReport, analyze_document, totals_of
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
            self.stage = f"criterion {self.done}/{len(self.progress)}"
        self.events.publish("criterion", {"criterion": criterion_id, **_progress_payload(self)})

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
        document_id: int,
        criteria: Sequence[str] | None,
        *,
        trace_id: str,
    ) -> JobState:
        """Queue an analysis and return its state. Never blocks."""
        selected = self.resolve_criteria(criteria)
        job = JobState(
            analysis_id=new_id(16),
            document_id=document_id,
            criteria=selected,
            trace_id=trace_id,
            events=Broadcast(self._settings.api_event_buffer),
            progress={cid: CriterionProgress(id=cid) for cid in selected},
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

    def cancel(self, job: JobState) -> None:
        """Ask the run to stop after the criteria that have already started."""
        job.cancelled.set()
        if job.future is not None and job.future.cancel():
            # Never picked up by the pool: nothing will ever set the terminal
            # state, so it is set here.
            job.finish(
                AnalysisReport(
                    analysis_id=job.analysis_id,
                    document_id=job.document_id,
                    status="cancelled",
                    trace_id=job.trace_id,
                    skipped=list(job.criteria),
                    totals=totals_of([]),
                    created_at=job.created_at,
                    completed_at=_now(),
                )
            )
        log.info("api.analysis.cancelled", extra={"analysis_id": job.analysis_id})

    # -- the worker -------------------------------------------------------

    def _run(self, job: JobState) -> None:
        """One analysis, on a pool thread, under the trace of the request that
        asked for it. Every exit path ends the job and closes its stream."""
        with trace_context(job.trace_id), span(
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
                )
            except BaseException as exc:  # noqa: BLE001 - a job must never die silently
                log.exception("api.analysis.failed", extra={"analysis_id": job.analysis_id})
                job.fail(f"{type(exc).__name__}: {exc}")
            else:
                job.finish(report)
            finally:
                conn.close()

    def _on_event(self, job: JobState, event: dict[str, Any]) -> None:
        """The runner calls this serially, so nothing here needs a lock of its
        own beyond what `JobState` already holds."""
        kind = event.get("type")
        criterion = event.get("criterion", "")
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


__all__ = ["JobRunner", "JobState"]
