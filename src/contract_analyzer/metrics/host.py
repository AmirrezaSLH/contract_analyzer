"""Host headroom: the latest sample, and the percent series over a window.

Tiles are the most recent `system_samples` row — current RAM and disk, not a
five-minute average. Charts take the last sample in each bucket so a disk
cliff is not smoothed into the hour it happened in.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from . import windows

_LATEST = """
SELECT rss_mb, rss_pct, disk_used_pct, disk_used_gb, disk_total_gb, ts
  FROM system_samples
 ORDER BY ts DESC
 LIMIT 1
"""

_BUCKET_LAST = """
WITH ranked AS (
    SELECT {bucket} AS k,
           rss_pct,
           disk_used_pct,
           row_number() OVER (PARTITION BY {bucket} ORDER BY ts DESC) AS rn
      FROM system_samples
     WHERE ts >= ?
)
SELECT k, rss_pct, disk_used_pct
  FROM ranked
 WHERE rn = 1
"""


def host_map(
    conn: sqlite3.Connection, *, window: str = "24h", now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    since = windows.since(window, now=now)
    latest = conn.execute(_LATEST).fetchone()
    series = _series(conn, window, since, now)
    return {
        "window": window,
        "since": since,
        "generated_at": now.isoformat(timespec="seconds"),
        "ts": None if latest is None else latest["ts"],
        "rss_mb": None if latest is None else latest["rss_mb"],
        "rss_pct": None if latest is None else latest["rss_pct"],
        "disk_used_pct": None if latest is None else latest["disk_used_pct"],
        "disk_used_gb": None if latest is None else latest["disk_used_gb"],
        "disk_total_gb": None if latest is None else latest["disk_total_gb"],
        "series": series,
    }


def _series(
    conn: sqlite3.Connection, window: str, since: str, now: datetime
) -> list[dict[str, Any]]:
    bucket = windows.bucket_for(window)
    expression = windows.bucket_expression("ts", bucket)
    found = {
        row["k"]: row
        for row in conn.execute(_BUCKET_LAST.format(bucket=expression), (since,))
    }
    out = []
    for start in windows.bucket_starts(window, bucket, now=now):
        row = found.get(start)
        out.append(
            {
                "bucket": start,
                "rss_pct": None if row is None else row["rss_pct"],
                "disk_used_pct": None if row is None else row["disk_used_pct"],
            }
        )
    return out


__all__ = ["host_map"]
