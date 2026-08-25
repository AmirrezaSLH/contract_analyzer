"""Where a span failed, not whether a run did.

The Monitor tab's Stages band is a `GROUP BY name` over `spans`. Nothing new
is recorded: ingest, retrieve, agent and chat already wrap themselves in
`span()`. The names are a fixed short list so a one-off `analysis.document`
error cannot become "the worst stage" of a quiet hour.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from . import windows

#: Pipeline steps the dashboard names. Not every span: `api.analysis` and
#: `ingest.file` are wrappers, and a quiet hour would otherwise report them.
STAGE_NAMES = (
    "ingest.parse",
    "ingest.embed",
    "retrieve",
    "agent.tool",
    "agent.call",
    "chat",
)

#: 1-of-1 is not a 100% error rate worth paging on.
MIN_SAMPLES = 10

#: Tiles look at this, not at the chart window.
LIVE = "5m"


def _names_sql() -> str:
    return "(" + ", ".join(f"'{name}'" for name in STAGE_NAMES) + ")"


_COUNTS = """
SELECT name,
       count(*) AS n,
       sum(status = 'error') AS errors
  FROM spans
 WHERE ts >= ? AND name IN {names}
 GROUP BY name
"""

_TOTALS = """
SELECT count(*) AS n,
       sum(status = 'error') AS errors
  FROM spans
 WHERE ts >= ? AND name IN {names}
"""

_BUCKET_COUNTS = """
SELECT {bucket} AS bucket,
       count(*) AS n,
       sum(status = 'error') AS errors
  FROM spans
 WHERE name = ? AND ts >= ?
 GROUP BY bucket
"""

_BUCKET_TOTALS = """
SELECT {bucket} AS bucket,
       count(*) AS n,
       sum(status = 'error') AS errors
  FROM spans
 WHERE name IN {names} AND ts >= ?
 GROUP BY bucket
"""


def stage_map(
    conn: sqlite3.Connection, *, window: str = "30m", now: datetime | None = None
) -> dict[str, Any]:
    """Live tiles over 5 minutes, charts over `window`, one worst name.

    The name is chosen from the live window when anything ran; otherwise from
    the chart window, so a quiet five minutes does not blank a 24 h chart.
    `errors_total` is every named stage in that same tile window, not just the
    worst name.
    """
    now = now or datetime.now(UTC)
    live_since = windows.since(LIVE, now=now)
    chart_since = windows.since(window, now=now)
    live_rows = _rows(conn, live_since)
    chart_rows = _rows(conn, chart_since)
    live_pick = _worst(live_rows)
    picked = live_pick or _worst(chart_rows)
    tile_rows = live_rows if live_pick else chart_rows
    tile_since = live_since if live_pick else chart_since
    tile = tile_rows.get(picked.name) if picked else None
    n_total, errors_total = _totals(conn, tile_since)
    series = _series(conn, None if picked is None else picked.name, window, chart_since, now)
    return {
        "window": window,
        "live_window": LIVE,
        "since": tile_since,
        "generated_at": now.isoformat(timespec="seconds"),
        "name": None if picked is None else picked.name,
        "n": 0 if tile is None else tile.n,
        "errors": 0 if tile is None else tile.errors,
        "error_rate": None if tile is None else _rate(tile.errors, tile.n),
        "errors_total": None if n_total == 0 else errors_total,
        "min_samples": MIN_SAMPLES,
        "series": series,
    }


class _Stage:
    __slots__ = ("name", "n", "errors")

    def __init__(self, name: str, n: int, errors: int) -> None:
        self.name = name
        self.n = n
        self.errors = errors

    @property
    def error_rate(self) -> float | None:
        return _rate(self.errors, self.n)


def _rows(conn: sqlite3.Connection, since: str) -> dict[str, _Stage]:
    out: dict[str, _Stage] = {}
    for row in conn.execute(_COUNTS.format(names=_names_sql()), (since,)):
        name = str(row["name"])
        out[name] = _Stage(name, int(row["n"] or 0), int(row["errors"] or 0))
    return out


def _totals(conn: sqlite3.Connection, since: str) -> tuple[int, int]:
    row = conn.execute(_TOTALS.format(names=_names_sql()), (since,)).fetchone()
    return int(row["n"] or 0), int(row["errors"] or 0)


def _worst(rows: dict[str, _Stage]) -> _Stage | None:
    if not rows:
        return None
    enough = [stage for stage in rows.values() if stage.n >= MIN_SAMPLES]
    pool = enough or list(rows.values())
    return max(pool, key=lambda stage: ((stage.error_rate or 0.0), stage.n))


def _series(
    conn: sqlite3.Connection,
    name: str | None,
    window: str,
    since: str,
    now: datetime,
) -> list[dict[str, Any]]:
    bucket = windows.bucket_for(window)
    expression = windows.bucket_expression("ts", bucket)
    counts = {
        row["bucket"]: row
        for row in conn.execute(
            _BUCKET_COUNTS.format(bucket=expression), (name or "", since)
        )
    }
    totals = {
        row["bucket"]: row
        for row in conn.execute(
            _BUCKET_TOTALS.format(bucket=expression, names=_names_sql()), (since,)
        )
    }
    out = []
    for start in windows.bucket_starts(window, bucket, now=now):
        row = counts.get(start)
        n = int(row["n"] or 0) if row is not None else 0
        errors = int(row["errors"] or 0) if row is not None else 0
        all_row = totals.get(start)
        n_all = int(all_row["n"] or 0) if all_row is not None else 0
        errors_all = int(all_row["errors"] or 0) if all_row is not None else 0
        out.append(
            {
                "bucket": start,
                "n": n,
                "error_rate": _rate(errors, n),
                "errors_total": None if n_all == 0 else errors_all,
            }
        )
    return out


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


__all__ = ["LIVE", "MIN_SAMPLES", "STAGE_NAMES", "stage_map"]
