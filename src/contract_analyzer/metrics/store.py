"""The metrics store: one object the surfaces hold, and what it will answer.

Phase 1 is a **query layer, not a second database**. `analyses` is already the
analysis fact table -- `schema.sql` holds it, `analyses.py` populates it on
completion -- so the store opens nothing of its own here and every read runs
on the connection the caller already has. A request has one; the API opens it
per request and closes it after, and a metrics read is not a reason to open a
second.

That is why the read methods take a connection rather than owning one. The
write path, when `spans` lands, is the opposite case and will own exactly one:
a writer thread cannot borrow a request's connection, and a request must not
wait for a writer.

`metrics/` imports `db`, `logger` and nothing above them. Nothing below it
imports `metrics` -- `analyses.py` and the API's storage must not depend on
telemetry to record what happened.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..config import Settings
from ..logger import get_logger
from . import queries

log = get_logger(__name__)


class MetricsStore:
    """What the KPI page reads. Built once per process, held on `app.state`."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # -- reads, on the caller's connection --------------------------------

    def summary(self, conn: sqlite3.Connection, *, window: str = "24h") -> dict[str, Any]:
        """The tiles and meters for one window. Live counts are the route's
        to add: they come from `JobRunner`, not from a table."""
        return queries.summary(conn, window=window)

    def timeseries(
        self, conn: sqlite3.Connection, *, window: str = "7d", bucket: str | None = None
    ) -> list[dict[str, Any]]:
        """The same numbers per bucket, oldest first, empty buckets included."""
        return queries.timeseries(conn, window=window, bucket=bucket)

    def runs(self, conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
        """The global runs table, newest first, each row carrying its trace id."""
        return queries.runs(conn, limit=limit)


__all__ = ["MetricsStore"]
