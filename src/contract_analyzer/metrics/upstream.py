"""Retries and exhausted calls through `http_client`, from `spans`.

`RetryingTransport` wraps every outbound request in `upstream.call` and
pulses `upstream.retry` / `upstream.failed` at the same site that logs
`http.retry` / `http.failed`. The Monitor band is a query over those names:
retries per 100 calls, exhausted rate, and the most common reason.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from . import windows

LIVE = "5m"

_CALL = "upstream.call"
_RETRY = "upstream.retry"
_FAILED = "upstream.failed"
_EVENTS = f"('{_RETRY}', '{_FAILED}')"


_COUNTS = """
SELECT
       sum(name = 'upstream.call')   AS calls,
       sum(name = 'upstream.retry')  AS retries,
       sum(name = 'upstream.failed') AS failed
  FROM spans
 WHERE ts >= ? AND name IN ('upstream.call', 'upstream.retry', 'upstream.failed')
"""

_TOP = """
SELECT json_extract(attrs, '$.reason') AS reason,
       count(*) AS n
  FROM spans
 WHERE ts >= ? AND name IN {events}
   AND json_extract(attrs, '$.reason') IS NOT NULL
 GROUP BY reason
 ORDER BY n DESC, reason
 LIMIT 1
"""

_EVENT_N = """
SELECT count(*) AS n
  FROM spans
 WHERE ts >= ? AND name IN {events}
"""

_BUCKET = """
SELECT {bucket} AS bucket,
       sum(name = 'upstream.call')   AS calls,
       sum(name = 'upstream.retry')  AS retries,
       sum(name = 'upstream.failed') AS failed
  FROM spans
 WHERE ts >= ? AND name IN ('upstream.call', 'upstream.retry', 'upstream.failed')
 GROUP BY bucket
"""


def upstream_map(
    conn: sqlite3.Connection, *, window: str = "30m", now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    live_since = windows.since(LIVE, now=now)
    chart_since = windows.since(window, now=now)
    live = _counts(conn, live_since)
    chart = _counts(conn, chart_since)
    tile_since = live_since if live["calls"] else chart_since
    tile = live if live["calls"] else chart
    reason, reason_n = _top(conn, tile_since)
    events = _events(conn, tile_since)
    series = _series(conn, window, chart_since, now)
    return {
        "window": window,
        "live_window": LIVE,
        "since": tile_since,
        "generated_at": now.isoformat(timespec="seconds"),
        "calls": tile["calls"],
        "retries": tile["retries"],
        "failed": tile["failed"],
        "retries_per_100": _per_100(tile["retries"], tile["calls"]),
        "exhausted_rate": _rate(tile["failed"], tile["calls"]),
        "top_reason": reason,
        "top_reason_share": _rate(reason_n, events),
        "series": series,
    }


def _counts(conn: sqlite3.Connection, since: str) -> dict[str, int]:
    row = conn.execute(_COUNTS, (since,)).fetchone()
    return {
        "calls": int(row["calls"] or 0),
        "retries": int(row["retries"] or 0),
        "failed": int(row["failed"] or 0),
    }


def _top(conn: sqlite3.Connection, since: str) -> tuple[str | None, int]:
    row = conn.execute(_TOP.format(events=_EVENTS), (since,)).fetchone()
    if row is None or not row["reason"]:
        return None, 0
    return str(row["reason"]), int(row["n"] or 0)


def _events(conn: sqlite3.Connection, since: str) -> int:
    row = conn.execute(_EVENT_N.format(events=_EVENTS), (since,)).fetchone()
    return int(row["n"] or 0)


def _series(
    conn: sqlite3.Connection, window: str, since: str, now: datetime
) -> list[dict[str, Any]]:
    bucket = windows.bucket_for(window)
    expression = windows.bucket_expression("ts", bucket)
    found = {
        row["bucket"]: row
        for row in conn.execute(_BUCKET.format(bucket=expression), (since,))
    }
    out = []
    for start in windows.bucket_starts(window, bucket, now=now):
        row = found.get(start)
        calls = int(row["calls"] or 0) if row is not None else 0
        retries = int(row["retries"] or 0) if row is not None else 0
        failed = int(row["failed"] or 0) if row is not None else 0
        out.append(
            {
                "bucket": start,
                "calls": calls,
                "retries": retries,
                "failed": failed,
                "retries_per_100": _per_100(retries, calls),
                "exhausted_rate": _rate(failed, calls),
            }
        )
    return out


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _per_100(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


__all__ = ["LIVE", "upstream_map"]
