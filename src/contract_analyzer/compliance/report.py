"""One contract against all five criteria: the runner, and what it produces.

`analyze_criterion` answers one question. This module is the layer above it --
the thing a CLI calls, the thing the API's job worker calls, and the only place
that knows a *document-level* analysis exists. It contains no prompting and no
model logic; it is a fan-out, a fan-in and a report.

Three things it is responsible for, all of them consequences of running five
agents at once:

**A connection per criterion, and the caller's is never touched.** `db.py`
says it plainly -- concurrent use of one connection from two threads is a bug,
and `check_same_thread=False` only stops sqlite3 from catching it. So the
database *path* is read once on the calling thread and each criterion opens its
own connection to it. Sharing would serialise every read anyway; SQLite gives
concurrent readers nothing on one connection. An in-memory database has no path
to reopen, so there the criteria run serially on the calling thread -- one
connection, no pool, and the honest amount of parallelism available.

**The trace id carried across the pool.** `trace_id` lives in a `ContextVar`,
and `ThreadPoolExecutor.submit` does not copy the calling context. Without
`copy_context().run`, every line the five agents emit -- `analysis.criterion`,
`agent.call`, `agent.tool`, every retry in the transport -- would carry a null
trace and the log would no longer reconstruct the run.

**Events that say which criterion they came from, delivered one at a time.**
The agent loop emits `tool_call` events with no criterion on them, because at
that level there is only one. Five interleaved runs would be unreadable, so the
runner tags each event before passing it on. It also holds a lock while calling
`on_event`, which means **a caller's callback is never invoked concurrently**
and never needs a lock of its own -- the CLI can print, the API can fan out to
its subscribers, neither has to think about it.

Cancellation is honest rather than aspirational: `cancelled()` is checked
before a criterion starts, so it skips whatever has not begun. At
`workers >= len(criteria)` everything starts at once and there is nothing left
to skip -- cancel then only stops a job still waiting for a free worker. Making
a *running* criterion stop would mean threading the flag into the agent loop,
between tool calls, and that is a change to `generation/`, not to this file.
"""

from __future__ import annotations

import contextvars
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..db import connect, describe_path
from ..documents import get_document
from ..embeddings.base import Embedder
from ..generation.agent import Event, OnEvent
from ..generation.analysis import analyze_criterion
from ..generation.client import get_client
from ..logger import get_logger, new_id, span, trace_context
from .criteria import Criterion, get_criteria
from .schemas import ComplianceResult

log = get_logger(__name__)

AnalysisStatus = Literal["done", "cancelled", "failed"]

#: Databases a second connection cannot be opened to. An in-memory database
#: *is* its connection: there is nothing to reopen, so those runs are serial.
_UNFORKABLE = frozenset({"", ":memory:"})


class AnalysisTotals(BaseModel):
    """What the run cost, summed over its criteria. The KPI page's row."""

    criteria: int = 0
    latency_s: float = 0.0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    #: Results the analyst flagged, or the validator capped. The share of a
    #: report a human still has to read.
    needs_review: int = 0
    #: Runs a counter stopped rather than the model finishing.
    capped: int = 0
    mean_confidence: float = 0.0


class AnalysisReport(BaseModel):
    """One contract, five verdicts, and everything needed to file the run.

    Persisted as JSON and returned over the wire unchanged -- there is no
    second schema between the disk and the API. `results` are in the order the
    criteria are defined, not the order they finished, so two runs of the same
    contract diff line by line.
    """

    analysis_id: str
    document_id: int
    filename: str = ""
    status: AnalysisStatus = "done"
    #: The trace every log line of this run carries. The demo's "here is the
    #: same id in app.jsonl" moment, and the join key for the metrics store.
    trace_id: str | None = None
    results: list[ComplianceResult] = Field(default_factory=list)
    totals: AnalysisTotals = Field(default_factory=AnalysisTotals)
    #: Contradictions between criteria -- "3 says encrypted in transit, 5 says
    #: no TLS requirement". The evaluator's cross-criterion pass fills this;
    #: until it lands the field exists and is empty, so the wire format does
    #: not change when it arrives. See plan_implement_docs/04_02.
    cross_criterion_notes: list[str] = Field(default_factory=list)
    #: Criteria that were asked for but never ran, because the run was
    #: cancelled while they were still queued.
    skipped: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: str = ""
    completed_at: str = ""

    @property
    def complete(self) -> bool:
        return self.status == "done" and not self.skipped


def analyze_document(
    document_id: int,
    conn: sqlite3.Connection,
    embedder: Embedder | None = None,
    settings: Settings | None = None,
    client: Any = None,
    *,
    criteria: Sequence[str] | None = None,
    on_event: OnEvent | None = None,
    cancelled: Callable[[], bool] | None = None,
    workers: int | None = None,
    analysis_id: str | None = None,
) -> AnalysisReport:
    """Assess one contract against every criterion, in parallel.

    `criteria` is a list of criterion ids, or None for all five. `on_event`
    receives the agents' events with a `criterion` key added, one at a time.
    `cancelled` is polled before each criterion starts. Raises `KeyError` for
    an unknown document or criterion id, and `AnswerUnavailable` before any
    request when there is no API key.
    """
    settings = settings or get_settings()
    client = client or get_client(settings)
    workers = workers or settings.analysis_workers
    analysis_id = analysis_id or new_id(16)

    document = get_document(conn, document_id)
    if document is None:
        raise KeyError(f"no document with id {document_id}")
    selected = _select(criteria)

    started = time.perf_counter()
    created_at = _now()
    emit = _Emitter(on_event)
    results: dict[str, ComplianceResult] = {}
    skipped: list[str] = []

    # Read on *this* thread: no worker ever touches the caller's connection.
    db_path = describe_path(conn)
    parallel = db_path not in _UNFORKABLE

    with (
        trace_context() as trace_id,
        span(
            "analysis.document",
            log,
            analysis_id=analysis_id,
            document_id=document_id,
            criteria=len(selected),
            workers=workers,
        ) as bag,
    ):
        def one(criterion: Criterion, target: sqlite3.Connection | str):
            return _run_one(
                criterion, document_id, target, embedder, settings, client, emit, cancelled
            )

        if parallel:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="criterion") as pool:
                futures = {
                    pool.submit(contextvars.copy_context().run, one, criterion, db_path): criterion
                    for criterion in selected
                }
                outcomes = [(futures[f], f.result()) for f in futures]
        else:
            outcomes = [(criterion, one(criterion, conn)) for criterion in selected]

        for criterion, result in outcomes:
            if result is None:
                skipped.append(criterion.id)
            else:
                results[criterion.id] = result

        ordered = [results[c.id] for c in selected if c.id in results]
        report = AnalysisReport(
            analysis_id=analysis_id,
            document_id=document_id,
            filename=document.filename,
            status="cancelled" if skipped else "done",
            trace_id=trace_id,
            results=ordered,
            totals=totals_of(ordered, latency_s=time.perf_counter() - started),
            skipped=skipped,
            created_at=created_at,
            completed_at=_now(),
        )
        bag.update(
            parallel=parallel,
            status=report.status,
            cost_usd=report.totals.cost_usd,
            needs_review=report.totals.needs_review,
            mean_confidence=report.totals.mean_confidence,
            skipped=len(skipped),
        )

    emit({"type": "report", "analysis_id": analysis_id, "status": report.status})
    return report


def totals_of(results: Sequence[ComplianceResult], *, latency_s: float = 0.0) -> AnalysisTotals:
    """Sum a run. Separate from the report so the API can total a partial one."""
    if not results:
        return AnalysisTotals(latency_s=round(latency_s, 3))
    return AnalysisTotals(
        criteria=len(results),
        latency_s=round(latency_s, 3),
        cost_usd=round(sum(r.cost_usd for r in results), 6),
        input_tokens=sum(r.usage.get("input_tokens", 0) for r in results),
        output_tokens=sum(r.usage.get("output_tokens", 0) for r in results),
        tool_calls=sum(r.tool_calls for r in results),
        needs_review=sum(1 for r in results if r.needs_review),
        capped=sum(1 for r in results if r.ended_by == "cap"),
        mean_confidence=round(sum(r.confidence for r in results) / len(results), 4),
    )


class _Emitter:
    """Serialises the callers' callback and stamps the criterion on every event.

    One lock for the whole run: the callback is the cheap end of a request that
    just spent seconds in the model, and serialising it means no consumer --
    the CLI's printer, the API's SSE fan-out -- needs a lock of its own.
    """

    def __init__(self, on_event: OnEvent | None) -> None:
        self._on_event = on_event
        self._lock = threading.Lock()

    def __call__(self, event: Event) -> None:
        if self._on_event is None:
            return
        with self._lock:
            self._on_event(event)

    def for_criterion(self, criterion_id: str) -> OnEvent:
        def emit(event: Event) -> None:
            self({**event, "criterion": criterion_id})

        return emit


def _run_one(
    criterion: Criterion,
    document_id: int,
    target: sqlite3.Connection | str,
    embedder: Embedder | None,
    settings: Settings,
    client: Any,
    emit: _Emitter,
    cancelled: Callable[[], bool] | None,
) -> ComplianceResult | None:
    """One criterion. `target` is a database path to open -- the parallel case,
    where this runs on a pool thread -- or the caller's own connection, for the
    serial one. Returns None when the run was cancelled before it started.
    """
    if cancelled is not None and cancelled():
        log.info("analysis.skipped", extra={"criterion": criterion.id, "reason": "cancelled"})
        emit({"type": "skipped", "criterion": criterion.id, "surface": "analysis"})
        return None

    own = connect(target, same_thread=False) if isinstance(target, str) else None
    try:
        return analyze_criterion(
            criterion,
            own if own is not None else target,
            embedder,
            settings,
            document_id=document_id,
            client=client,
            on_event=emit.for_criterion(criterion.id),
        )
    finally:
        if own is not None:
            own.close()


def _select(criteria: Sequence[str] | None) -> tuple[Criterion, ...]:
    """The criteria to run, in their defined order whatever order was asked."""
    everything = get_criteria()
    if criteria is None:
        return everything
    wanted = set(criteria)
    unknown = wanted - {c.id for c in everything}
    if unknown:
        raise KeyError(f"unknown criterion id(s): {', '.join(sorted(unknown))}")
    return tuple(c for c in everything if c.id in wanted)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


__all__ = [
    "AnalysisReport",
    "AnalysisTotals",
    "analyze_document",
    "totals_of",
]
