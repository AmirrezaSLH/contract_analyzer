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
from dataclasses import dataclass, field
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


@dataclass(frozen=True, kw_only=True)
class LastAnalysis:
    """The most recent analysis of one document, as a library row needs it.

    Not an `AnalysisRecord`: this is the projection a *list* wants -- enough to
    draw a chip beside a document and nothing more -- and it is built by one
    query over every document rather than by reading a report per row. The
    counts are the whole point. A list endpoint that returned "5 of 5
    compliant" would be choosing this UI's words for every other consumer;
    `states` lets each one choose its own.
    """

    analysis_id: str
    document_id: int
    status: str
    completed_at: str | None = None
    #: `{"Fully Compliant": 5, ...}`, from the stored report. Empty for a run
    #: that never produced one -- which is every status except `done` and a
    #: `cancelled` run that got some of the way.
    states: dict[str, int] = field(default_factory=dict)
    needs_review: int = 0


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
    # After the run's own row, and never able to fail it: the report is the
    # deliverable and the per-criterion table is history about it.
    record_criteria(conn, analysis_id, report)
    return cursor.rowcount > 0


#: The per-criterion table, written beside the run's own row. Its DDL lives in
#: `metrics/metrics.sql` -- it is telemetry, not a domain object -- which is
#: why this INSERT is guarded rather than assumed: a process that never built a
#: metrics store has no such table, and an analysis must not fail to record
#: itself because nobody asked for a dashboard.
_CRITERION_RESULTS = """
INSERT OR REPLACE INTO criterion_results (
    run_id, criterion_id, state, confidence, raw_confidence, needs_review,
    ended_by, structure_rounds, tool_calls, cost_usd, quotes_total,
    quotes_verified, latency_s
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def record_criteria(
    conn: sqlite3.Connection, analysis_id: str, report: AnalysisReport
) -> int:
    """One row per criterion of this run, for the state-mix and drift queries.

    Called by `finish_analysis`, which is already holding the report: the
    alternative is mining `report_json` with `json_each` on every query, or a
    backfill later. Rows are `INSERT OR REPLACE`, so a backfill over old
    reports is the same statement.

    **A missing table is not an error here.** `criterion_results` is created by
    the metrics store, and this module must not import it -- storage does not
    depend on telemetry. A process that never built a store simply records no
    per-criterion history, and says so at debug level.
    """
    rows = [
        (
            analysis_id,
            result.criterion_id,
            result.compliance_state,
            result.confidence,
            result.raw_confidence,
            int(result.needs_review),
            result.ended_by,
            result.structure_rounds,
            result.tool_calls,
            result.cost_usd,
            len(result.relevant_quotes),
            sum(1 for quote in result.relevant_quotes if quote.verified),
            result.latency_s,
        )
        for result in report.results
    ]
    if not rows:
        return 0
    try:
        with conn:
            conn.executemany(_CRITERION_RESULTS, rows)
    except sqlite3.OperationalError as exc:
        log.debug("analyses.criteria_unrecorded", extra={"error": str(exc)})
        return 0
    return len(rows)


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

    `created_at` has one-second resolution, and `analysis_id` is random hex, so
    the tie-break is `rowid`: it is assigned in insert order, which makes two
    submissions in the same second come back in the order they were made
    rather than merely in a stable one. A library row showing "the last
    analysis" has to be the last one, not the one whose id happens to sort
    highest.
    """
    sql = f"SELECT {_COLUMNS} FROM analyses"
    params: list[Any] = []
    if document_id is not None:
        sql += " WHERE document_id = ?"
        params.append(int(document_id))
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
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


#: The newest analysis of every document, one row per (document, state) pair.
#: The pick of *which* analysis is the newest happens in SQL rather than in
#: Python because `GET /documents` is read by a UI that re-renders on every
#: click, and a lookup per row there is an N+1 that grows with the corpus.
#: `json_each` counts the compliance states off the stored report, which keeps
#: ~30 KB of JSON per document out of a path that wants five integers. The
#: LEFT JOIN is load-bearing: a run with no report has no `results` array, and
#: it must still come back carrying its status.
_LAST_ANALYSIS = """
WITH latest AS (
    SELECT a.analysis_id, a.document_id, a.status, a.completed_at,
           a.needs_review, a.report_json
      FROM analyses a
     WHERE a.analysis_id = (
           SELECT b.analysis_id FROM analyses b
            WHERE b.document_id = a.document_id
            ORDER BY b.created_at DESC, b.rowid DESC LIMIT 1)
       {filter}
)
SELECT l.document_id, l.analysis_id, l.status, l.completed_at, l.needs_review,
       json_extract(r.value, '$.compliance_state') AS state,
       count(r.value) AS n
  FROM latest l
  LEFT JOIN json_each(l.report_json, '$.results') r
 GROUP BY l.document_id, state
"""


def last_analysis_by_document(
    conn: sqlite3.Connection, document_ids: Sequence[int] | None = None
) -> dict[int, LastAnalysis]:
    """The newest analysis of each document, keyed by `document_id`.

    One query for the whole library. Documents that have never been analysed
    are simply absent from the mapping, which is what `last_analysis: null`
    means on the wire and what draws the "Not analysed" chip.
    """
    params: list[Any] = []
    filter_sql = ""
    if document_ids is not None:
        ids = [int(d) for d in document_ids]
        if not ids:
            return {}
        filter_sql = f"AND a.document_id IN ({','.join('?' * len(ids))})"
        params = list(ids)

    rows = conn.execute(_LAST_ANALYSIS.format(filter=filter_sql), params).fetchall()

    states: dict[int, dict[str, int]] = {}
    heads: dict[int, sqlite3.Row] = {}
    for row in rows:
        document_id = int(row["document_id"])
        heads[document_id] = row
        if row["state"]:
            states.setdefault(document_id, {})[row["state"]] = int(row["n"])
    return {
        document_id: LastAnalysis(
            analysis_id=row["analysis_id"],
            document_id=document_id,
            status=row["status"],
            completed_at=row["completed_at"],
            states=states.get(document_id, {}),
            needs_review=int(row["needs_review"] or 0),
        )
        for document_id, row in heads.items()
    }


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
    "LastAnalysis",
    "fail_analysis",
    "finish_analysis",
    "get_analysis",
    "last_analysis_by_document",
    "list_analyses",
    "live_analyses",
    "mark_running",
    "queue_analysis",
    "reconcile",
    "record_criteria",
]
