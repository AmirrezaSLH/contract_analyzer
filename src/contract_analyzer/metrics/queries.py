"""The KPI numbers, as SQL over `analyses` and `documents`.

Phase 1 of `plan_implement_docs/KPI_01/Metric_Store.md`: everything on the
dashboard's first cut is already in the analysis fact table, so this is a
query layer and not a second store. `analyses` is written by `analyses.py` on
completion -- `latency_s`, `cost_usd`, the token and quote counts,
`needs_review`, `capped`, `mean_confidence`, `surface` -- and nothing here
writes anything.

Three rules the numbers follow, each of which is a decision from
`01_findings.md` rather than a detail:

* **Three outcomes, not two.** `failed`, `interrupted` and done-but-
  `needs_review` stay apart. The first two are reliability, the third is
  quality, and a failure rate that absorbs the third under-counts what a
  reviewer should care about.
* **Percentiles, not averages, wherever the number can alarm.** The mean is
  reported beside p50 and p95 as context and never on its own; the tail is
  what breaks a demo and the mean hides it.
* **Rates carry their own denominators.** A quote-verification rate with no
  `quotes_total` beside it cannot be told apart from a run that produced no
  quotes at all, and the two mean opposite things.

The evaluator columns on `analyses` are `NULL` until the evaluator lands, so
the accept-rate slot reports **cap rate** and says in the payload that it is
doing so. A tile that invents a number nobody computed is worse than an empty
one.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from . import windows

#: Statuses that reached an outcome. The denominator of every reliability
#: rate: a run still queued has not failed, and dividing by it would make the
#: failure rate fall every time somebody submitted work.
SETTLED = ("done", "failed", "interrupted", "cancelled")

_SUMMARY = """
SELECT count(*)                                          AS runs,
       sum(status = 'done')                              AS done,
       sum(status = 'failed')                            AS failed,
       sum(status = 'interrupted')                       AS interrupted,
       sum(status = 'cancelled')                         AS cancelled,
       sum(status IN ('queued', 'running'))              AS live,
       sum(coalesce(criteria_completed, 0))              AS criteria,
       sum(coalesce(cost_usd, 0))                        AS cost_total,
       avg(cost_usd)                                     AS cost_mean,
       avg(latency_s)                                    AS latency_mean,
       sum(coalesce(input_tokens, 0))                    AS input_tokens,
       sum(coalesce(output_tokens, 0))                   AS output_tokens,
       sum(coalesce(tool_calls, 0))                      AS tool_calls,
       sum(coalesce(quotes_total, 0))                    AS quotes_total,
       sum(coalesce(quotes_verified, 0))                 AS quotes_verified,
       sum(coalesce(needs_review, 0))                    AS needs_review,
       sum(coalesce(capped, 0))                          AS capped,
       sum(needs_review > 0)                             AS runs_needing_review,
       sum(capped > 0)                                   AS runs_capped,
       avg(mean_confidence)                              AS mean_confidence
  FROM analyses
 WHERE created_at >= ?
"""

#: Nearest-rank percentiles, per `Metric_Store.md` §3: `row_number()` against
#: `count(*) over ()`, no rows fetched into Python to be sorted. The rank is
#: `ceil(n * p / 100)` written as integer arithmetic -- `(n * p + 99) / 100` --
#: because SQLite's `ceil()` is a compile-time option and the interpreter this
#: runs on is whichever one Python was built against. At n=1 both percentiles
#: are the one value; at n=2 p50 is the lower and p95 the upper, which is the
#: edge percentile arithmetic usually gets wrong.
#:
#: `{key}` is either a literal `''` -- the whole window as one group -- or a
#: bucket expression, so one statement serves the tiles and the charts.
_PERCENTILES = """
WITH ordered AS (
    SELECT {key} AS k,
           {value} AS v,
           row_number() OVER (PARTITION BY {key} ORDER BY {value}) AS rn,
           count(*)    OVER (PARTITION BY {key})                   AS n
      FROM analyses
     WHERE created_at >= ? AND {value} IS NOT NULL AND status IN {settled}
)
SELECT k,
       max(CASE WHEN rn = (n * 50 + 99) / 100 THEN v END) AS p50,
       max(CASE WHEN rn = (n * 95 + 99) / 100 THEN v END) AS p95
  FROM ordered
 GROUP BY k
"""

_BY_SURFACE = """
SELECT surface, count(*) AS runs, sum(coalesce(cost_usd, 0)) AS cost_usd
  FROM analyses
 WHERE created_at >= ?
 GROUP BY surface
 ORDER BY runs DESC
"""

_PER_BUCKET = """
SELECT {bucket}                              AS bucket,
       count(*)                              AS runs,
       sum(status = 'done')                  AS done,
       sum(status IN ('failed', 'interrupted')) AS failed,
       sum(coalesce(cost_usd, 0))            AS cost_usd,
       sum(coalesce(criteria_completed, 0))  AS criteria,
       sum(coalesce(quotes_total, 0))        AS quotes_total,
       sum(coalesce(quotes_verified, 0))     AS quotes_verified,
       sum(coalesce(needs_review, 0))        AS needs_review,
       avg(mean_confidence)                  AS mean_confidence
  FROM analyses
 WHERE created_at >= ?
 GROUP BY bucket
"""

#: The compliance-state mix per bucket, mined out of the stored report the way
#: the library's document list already mines `last_analysis`. It is the
#: historical half of the drift question -- the same contract flipping state --
#: until `criterion_results` makes it a first-class column.
_STATES_PER_BUCKET = """
SELECT {bucket} AS bucket,
       json_extract(r.value, '$.compliance_state') AS state,
       count(*) AS n
  FROM analyses a
  JOIN json_each(a.report_json, '$.results') r
 WHERE a.created_at >= ?
 GROUP BY bucket, state
"""

_RUNS = """
SELECT analysis_id, trace_id, document_id, filename, surface, status,
       criteria_requested, criteria_completed, criteria_skipped, error,
       created_at, started_at, completed_at,
       latency_s, cost_usd, input_tokens, output_tokens, tool_calls,
       needs_review, capped, mean_confidence, quotes_total, quotes_verified
  FROM analyses
 ORDER BY created_at DESC, rowid DESC
 LIMIT ?
"""


def summary(
    conn: sqlite3.Connection, *, window: str = "24h", now: datetime | None = None
) -> dict[str, Any]:
    """Every tile and meter on the KPI page, for one window.

    The live half -- workers busy, queued -- is deliberately absent: it comes
    from `JobRunner`, not from a table, and the route joins the two.
    """
    now = now or datetime.now(UTC)
    since = windows.since(window, now=now)
    row = conn.execute(_SUMMARY, (since,)).fetchone()
    latency = _percentiles(conn, "latency_s", since).get("", (None, None))
    cost = _percentiles(conn, "cost_usd", since).get("", (None, None))

    runs = int(row["runs"] or 0)
    settled = sum(int(row[status] or 0) for status in SETTLED)
    broken = int(row["failed"] or 0) + int(row["interrupted"] or 0)
    criteria = int(row["criteria"] or 0)
    quotes_total = int(row["quotes_total"] or 0)

    return {
        "window": window,
        "since": since,
        "generated_at": now.isoformat(timespec="seconds"),
        "runs": {
            "total": runs,
            "settled": settled,
            "done": int(row["done"] or 0),
            "failed": int(row["failed"] or 0),
            "interrupted": int(row["interrupted"] or 0),
            "cancelled": int(row["cancelled"] or 0),
            "live": int(row["live"] or 0),
            "criteria": criteria,
        },
        "reliability": {
            # `failed` and `interrupted` together, and neither of them is
            # done-but-needs-review: that one is on the quality meter.
            "failure_rate": _rate(broken, settled),
            "failed": int(row["failed"] or 0),
            "interrupted": int(row["interrupted"] or 0),
        },
        "latency_s": {
            "p50": _round(latency[0], 3),
            "p95": _round(latency[1], 3),
            "mean": _round(row["latency_mean"], 3),
        },
        "cost_usd": {
            "total": _round(row["cost_total"], 6) or 0.0,
            "mean": _round(row["cost_mean"], 6),
            "p50": _round(cost[0], 6),
            "p95": _round(cost[1], 6),
        },
        "tokens": {
            "input": int(row["input_tokens"] or 0),
            "output": int(row["output_tokens"] or 0),
            "tool_calls": int(row["tool_calls"] or 0),
        },
        "quality": {
            "quote_verification_rate": _rate(int(row["quotes_verified"] or 0), quotes_total),
            "quotes_total": quotes_total,
            "quotes_verified": int(row["quotes_verified"] or 0),
            "needs_review_rate": _rate(int(row["needs_review"] or 0), criteria),
            "needs_review": int(row["needs_review"] or 0),
            "runs_needing_review": int(row["runs_needing_review"] or 0),
            "mean_confidence": _round(row["mean_confidence"], 4),
            "cap_rate": _rate(int(row["capped"] or 0), criteria),
            "capped": int(row["capped"] or 0),
            "runs_capped": int(row["runs_capped"] or 0),
            "evaluator": _evaluator_slot(_rate(int(row["capped"] or 0), criteria)),
        },
        "surfaces": [
            {
                "surface": surface["surface"],
                "runs": int(surface["runs"]),
                "cost_usd": _round(surface["cost_usd"], 6) or 0.0,
            }
            for surface in conn.execute(_BY_SURFACE, (since,))
        ],
        "documents": int(conn.execute("SELECT count(*) FROM documents").fetchone()[0]),
    }


def timeseries(
    conn: sqlite3.Connection,
    *,
    window: str = "7d",
    bucket: str | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """The same numbers per bucket, oldest first, empty buckets included."""
    now = now or datetime.now(UTC)
    bucket = bucket or windows.bucket_for(window)
    since = windows.since(window, now=now)
    expression = windows.bucket_expression("created_at", bucket)

    rows = {
        row["bucket"]: row
        for row in conn.execute(_PER_BUCKET.format(bucket=expression), (since,))
    }
    latency = _percentiles(conn, "latency_s", since, key=expression)
    cost = _percentiles(conn, "cost_usd", since, key=expression)
    states: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        _STATES_PER_BUCKET.format(bucket=windows.bucket_expression("a.created_at", bucket)),
        (since,),
    ):
        if row["state"]:
            states.setdefault(row["bucket"], {})[row["state"]] = int(row["n"])

    series = []
    for start in windows.bucket_starts(window, bucket, now=now):
        row = rows.get(start)
        criteria = int(row["criteria"] or 0) if row else 0
        series.append(
            {
                "bucket": start,
                "runs": int(row["runs"]) if row else 0,
                "done": int(row["done"] or 0) if row else 0,
                "failed": int(row["failed"] or 0) if row else 0,
                "cost_usd": _round(row["cost_usd"], 6) if row else 0.0,
                "latency_s": {
                    "p50": _round(latency.get(start, (None, None))[0], 3),
                    "p95": _round(latency.get(start, (None, None))[1], 3),
                },
                "cost_percentiles": {
                    "p50": _round(cost.get(start, (None, None))[0], 6),
                    "p95": _round(cost.get(start, (None, None))[1], 6),
                },
                "mean_confidence": _round(row["mean_confidence"], 4) if row else None,
                "quote_verification_rate": (
                    _rate(int(row["quotes_verified"] or 0), int(row["quotes_total"] or 0))
                    if row
                    else None
                ),
                "needs_review_rate": (
                    _rate(int(row["needs_review"] or 0), criteria) if row else None
                ),
                "states": states.get(start, {}),
            }
        )
    return series


def runs(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    """The global runs table, newest first -- the list `GET /analyses` does
    not serve, because that one is per document on purpose.

    `trace_id` is on every row and is the reason the table exists at all: it
    is what turns a number on this page into a grep in `.run/app.jsonl`.
    `report_json` is not selected; a runs table wants none of thirty kilobytes
    of report per row.
    """
    return [dict(row) for row in conn.execute(_RUNS, (int(limit),))]


def _percentiles(
    conn: sqlite3.Connection, value: str, since: str, *, key: str = "''"
) -> dict[str, tuple[float | None, float | None]]:
    sql = _PERCENTILES.format(key=key, value=value, settled=_in(SETTLED))
    return {row["k"]: (row["p50"], row["p95"]) for row in conn.execute(sql, (since,))}


def _evaluator_slot(cap_rate: float | None) -> dict[str, Any]:
    """The accept/revise/fallback meter, honestly empty.

    `analyses.evaluator_*` are declared and `NULL` until the evaluator lands,
    so this reports what is actually being shown in that slot rather than
    letting a UI label a cap rate as an accept rate.
    """
    return {
        "available": False,
        "accept_rate": None,
        "showing": "cap_rate",
        "value": cap_rate,
        "note": (
            "The evaluator has not landed, so its columns are NULL. Cap rate "
            "-- results a counter stopped rather than the model finishing -- "
            "stands in for the accept rate and swaps out cleanly."
        ),
    }


def _in(values: tuple[str, ...]) -> str:
    """A literal IN list. The values are this module's own constants."""
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"


def _rate(part: int, whole: int) -> float | None:
    """A rate, or None when nothing was measured. Never 0.0 for an empty set:
    a zero verification rate and no quotes at all mean opposite things."""
    return round(part / whole, 4) if whole else None


def _round(value: Any, places: int) -> float | None:
    return None if value is None else round(float(value), places)


__all__ = ["SETTLED", "runs", "summary", "timeseries"]
