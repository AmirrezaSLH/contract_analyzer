"""The analysis record: what was run, how it went, and the report it produced.

`documents.py` is the catalogue of contracts; this is the catalogue of the work
done on them. Same layer, same shape -- plain functions over a connection, no
framework, importable by the CLI -- and it exists because without it an
analysis lived only in a dict on one process. A restart lost the report, a
second uvicorn worker could not see its neighbour's runs, and a row left
`running` by a killed process was a client polling forever.

**Both surfaces write here, and `report.py` is what does it.** `analyze_document`
calls `mark_running` on entry and `finish_analysis` / `fail_analysis` on exit,
so `make analyze` populates the same table the API does and a report produced
from the command line is readable through `GET /analyses/{id}`. The API adds
one thing the CLI has no equivalent for -- accepted but not yet started -- and
that is the whole of `queue_analysis`. `mark_running` is an upsert precisely so
both paths work: with a queued row it transitions it, without one it creates it.

**`interrupted` is a status, not a flavour of `failed`.** A row left `running`
by a process that went away is not a run the model refused. The two want
different KPI treatment and different copy -- "this analysis failed" against
"this analysis was interrupted; run it again" -- so `reconcile`, called once at
startup before anything is served, writes the distinction down.

**No foreign key to `documents`, and `filename` is denormalised.** Deleting a
contract must not delete the analyses of it. The report is the deliverable and
it is self-contained; a record that disappears because someone tidied up the
corpus is the opposite of a record. `tests/test_analyses.py` asserts the
outcome rather than trusting that nobody adds the constraint later.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .logger import get_logger

if TYPE_CHECKING:  # `report.py` imports this module; the cycle is broken here
    from .report import AnalysisReport

log = get_logger(__name__)

#: Statuses that are not an outcome yet. `reconcile` rewrites these at startup,
#: and a delete of the document underneath one is refused while it holds.
LIVE: frozenset[str] = frozenset({"queued", "running"})

_COLUMNS = """
analysis_id, trace_id, document_id, filename, surface, status,
criteria_requested, criteria_completed, criteria_skipped, error, report_json,
created_at, started_at, completed_at,
latency_s, cost_usd, input_tokens, output_tokens, tool_calls, needs_review,
capped, mean_confidence, quotes_total, quotes_verified,
evaluator_accepted, evaluator_revised, evaluator_fallback
"""


@dataclass(frozen=True, kw_only=True)
class AnalysisRecord:
    """One row of `analyses`.

    Named `AnalysisRecord` rather than `Analysis` because `api.schemas.Analysis`
    is the wire type for the same thing and the two meet in `api/jobs.py`; one
    name for both would be an alias at every import.

    `report_json` is carried as text and parsed on demand: a list endpoint reads
    a hundred of these and wants none of the reports.
    """

    analysis_id: str
    document_id: int
    status: str = "queued"
    filename: str = ""
    surface: str = "api"
    trace_id: str | None = None
    criteria_requested: int = 0
    criteria_completed: int = 0
    criteria_skipped: int = 0
    error: str | None = None
    report_json: str | None = None
    created_at: str = ""
    started_at: str | None = None
    completed_at: str | None = None
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
    evaluator_accepted: int | None = None
    evaluator_revised: int | None = None
    evaluator_fallback: int | None = None

    @property
    def live(self) -> bool:
        return self.status in LIVE

    def report(self) -> AnalysisReport | None:
        """The stored report, parsed. `None` when the run never produced one.

        The import is local: `report.py` imports this module to write the
        lifecycle, so a module-level import would close the loop.
        """
        if not self.report_json:
            return None
        from .report import AnalysisReport

        return AnalysisReport.model_validate_json(self.report_json)


# -- writes ----------------------------------------------------------------


def queue_analysis(
    conn: sqlite3.Connection,
    analysis_id: str,
    document_id: int,
    *,
    filename: str = "",
    criteria: Sequence[str] = (),
    trace_id: str | None = None,
    surface: str = "api",
) -> None:
    """Record an analysis that has been accepted but not started.

    The one state the API has and the CLI does not, which is why this is the
    only lifecycle call `JobRunner` makes and every other one lives in
    `report.py`.
    """
    with conn:
        conn.execute(
            "INSERT INTO analyses (analysis_id, trace_id, document_id, filename, surface, "
            "status, criteria_requested, created_at) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
            (analysis_id, trace_id, int(document_id), filename, surface, len(criteria), _now()),
        )


def mark_running(
    conn: sqlite3.Connection,
    analysis_id: str,
    *,
    document_id: int,
    filename: str = "",
    criteria: Sequence[str] = (),
    trace_id: str | None = None,
    surface: str = "cli",
) -> None:
    """Move an analysis to `running`, creating the row if there is none.

    An upsert rather than an UPDATE because the CLI never queued: `make analyze`
    goes straight from nothing to running, and it must still get a row. On the
    conflict path `surface` and `created_at` are left as the queueing surface
    wrote them -- an API run does not become a CLI one because the worker
    started it.
    """
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO analyses (analysis_id, trace_id, document_id, filename, surface, "
            "status, criteria_requested, created_at, started_at) "
            "VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?) "
            "ON CONFLICT(analysis_id) DO UPDATE SET "
            "  status = 'running', "
            "  started_at = excluded.started_at, "
            "  filename = CASE WHEN analyses.filename = '' THEN excluded.filename "
            "                  ELSE analyses.filename END, "
            "  trace_id = COALESCE(excluded.trace_id, analyses.trace_id), "
            "  criteria_requested = excluded.criteria_requested",
            (
                analysis_id, trace_id, int(document_id), filename, surface,
                len(criteria), now, now,
            ),
        )


def finish_analysis(
    conn: sqlite3.Connection, analysis_id: str, report: AnalysisReport
) -> bool:
    """Record the outcome, the report, and everything derivable from it.

    The derived columns are filled here rather than left to the metrics store
    because this function is already holding the report in order to count
    `criteria_completed` and `criteria_skipped`. The totals are field reads and
    the quote counts are one comprehension; the alternative is a backfill later.
    """
    totals = report.totals
    quotes = [quote for result in report.results for quote in result.relevant_quotes]
    with conn:
        cursor = conn.execute(
            "UPDATE analyses SET status = ?, error = ?, report_json = ?, completed_at = ?, "
            "criteria_completed = ?, criteria_skipped = ?, latency_s = ?, cost_usd = ?, "
            "input_tokens = ?, output_tokens = ?, tool_calls = ?, needs_review = ?, "
            "capped = ?, mean_confidence = ?, quotes_total = ?, quotes_verified = ? "
            "WHERE analysis_id = ?",
            (
                report.status,
                report.error,
                report.model_dump_json(),
                report.completed_at or _now(),
                len(report.results),
                len(report.skipped),
                totals.latency_s,
                totals.cost_usd,
                totals.input_tokens,
                totals.output_tokens,
                totals.tool_calls,
                totals.needs_review,
                totals.capped,
                totals.mean_confidence,
                len(quotes),
                sum(1 for quote in quotes if quote.verified),
                analysis_id,
            ),
        )
    return cursor.rowcount > 0


def fail_analysis(conn: sqlite3.Connection, analysis_id: str, error: str) -> bool:
    """Record a run that raised. No report: there is nothing to hand back.

    Terminal rows are left alone, so the API's own belt-and-braces call after
    `analyze_document` has already failed the row does not overwrite anything.
    """
    with conn:
        cursor = conn.execute(
            "UPDATE analyses SET status = 'failed', error = ?, completed_at = ? "
            "WHERE analysis_id = ? AND status IN ('queued', 'running')",
            (error, _now(), analysis_id),
        )
    return cursor.rowcount > 0


def reconcile(conn: sqlite3.Connection) -> int:
    """Close out rows a dead process left open. Returns how many.

    Called once from the API's lifespan, before the app serves anything.
    `interrupted` rather than `failed`: nothing refused, the machine went away,
    and the client should be told to run it again.
    """
    with conn:
        cursor = conn.execute(
            "UPDATE analyses SET status = 'interrupted', completed_at = ? "
            "WHERE status IN ('queued', 'running')",
            (_now(),),
        )
    count = cursor.rowcount
    if count:
        log.info("analyses.reconciled", extra={"interrupted": count})
    return count


# -- reads -----------------------------------------------------------------


def get_analysis(conn: sqlite3.Connection, analysis_id: str) -> AnalysisRecord | None:
    """One analysis, or None -- like `get_document`, because every caller turns
    "no such id" into its own answer."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM analyses WHERE analysis_id = ?", (analysis_id,)
    ).fetchone()
    return _record(row) if row is not None else None


def list_analyses(
    conn: sqlite3.Connection,
    document_id: int | None = None,
    *,
    limit: int = 50,
) -> list[AnalysisRecord]:
    """Analyses, newest first, optionally for one document.

    `created_at` has one-second resolution, so `analysis_id` breaks the tie and
    two submissions in the same second still come back in a stable order.
    """
    sql = f"SELECT {_COLUMNS} FROM analyses"
    params: list[Any] = []
    if document_id is not None:
        sql += " WHERE document_id = ?"
        params.append(int(document_id))
    sql += " ORDER BY created_at DESC, analysis_id DESC LIMIT ?"
    params.append(int(limit))
    return [_record(row) for row in conn.execute(sql, params)]


def live_analyses(conn: sqlite3.Connection, document_id: int) -> list[AnalysisRecord]:
    """Analyses of one document that are still queued or running.

    What `DELETE /documents/{id}` checks. A query rather than a look in the job
    dict, so a run owned by *another* worker also blocks the delete.
    """
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM analyses WHERE document_id = ? AND status IN ('queued', "
        "'running') ORDER BY created_at DESC",
        (int(document_id),),
    ).fetchall()
    return [_record(row) for row in rows]


def _record(row: sqlite3.Row) -> AnalysisRecord:
    # `dict(row)` rather than a comprehension: sqlite3.Row exposes keys() and
    # __getitem__, which is the mapping protocol dict() reads, and iterating a
    # Row directly would yield its values.
    return AnalysisRecord(**dict(row))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


__all__ = [
    "LIVE",
    "AnalysisRecord",
    "fail_analysis",
    "finish_analysis",
    "get_analysis",
    "list_analyses",
    "live_analyses",
    "mark_running",
    "queue_analysis",
    "reconcile",
]
