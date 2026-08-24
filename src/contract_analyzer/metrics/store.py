"""The metrics store: the tables, the writer behind them, and the queries.

Two halves, and they are asymmetric on purpose.

**The reads run on the caller's connection.** `analyses` is already the
analysis fact table -- `schema.sql` holds it, `analyses.py` populates it on
completion -- so phase 1's tiles needed no storage at all, and a request that
already has a connection open should not make the metrics layer open a second.

**The write side owns exactly one connection, on one thread.** A writer thread
cannot borrow a request's connection, and a request must never wait for a
writer. `record_span` is not called by application code at all: the handler in
`handler.py` turns `span.end` log records into rows, which is why no module
that emits a span had to learn this one exists.

`metrics.sql` is applied when the store is built -- `CREATE TABLE IF NOT
EXISTS`, so an old database just grows the tables. It is a second DDL *file*
and not a second database: one file on disk remains the whole storage story,
and `db.py` runs `schema.sql` through `str.format(dim=...)`, so span DDL
containing `json_extract(attrs, '$.model')` could not live there.

`metrics/` imports `db`, `logger` and nothing above them. Nothing below it
imports `metrics`: `analyses.py` and the API's storage must not depend on
telemetry to record what happened.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ..config import Settings
from ..db import connect
from ..logger import attach_handler, detach_handler, get_logger
from . import queries
from .handler import SpanHandler

log = get_logger(__name__)

METRICS_SQL = Path(__file__).with_name("metrics.sql")


class MetricsStore:
    """What the KPI page reads and what the span handler writes.

    One per process, built in the API's lifespan and by `scripts/analyze.py`,
    so the command line populates the same tables the API does. A dashboard
    that only sees HTTP traffic measures the surface, not the system.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._path = settings.db_path
        self.handler = SpanHandler(self._open)
        self._installed = False
        # Applied here, on a connection of this thread's, so that `spans`
        # exists before the first request queries it -- the writer thread's
        # connection may not have been opened yet.
        self.apply_schema()

    # -- lifecycle --------------------------------------------------------

    def apply_schema(self) -> None:
        conn = self._open()
        try:
            conn.executescript(METRICS_SQL.read_text(encoding="utf-8"))
            conn.commit()
        finally:
            conn.close()

    def install(self) -> MetricsStore:
        """Attach the handler to the project's root logger and start writing.

        Separate from the constructor so that building a store to *read* from
        -- a test, a script -- does not start a thread.
        """
        if not self._installed:
            attach_handler(self.handler)
            self.handler.start()
            self._installed = True
        return self

    def close(self) -> None:
        """Stop recording, flush what is queued, close the writer. Idempotent."""
        if self._installed:
            detach_handler(self.handler)
            self._installed = False
        self.handler.close()

    def _open(self) -> sqlite3.Connection:
        """The writer's connection. `same_thread=False` because it is created
        on whichever thread asked and used by the writer thread -- one at a
        time, never both."""
        return connect(self._path, same_thread=False)

    # -- writes -----------------------------------------------------------

    @property
    def dropped(self) -> int:
        """Spans thrown away rather than recorded. Reported by `summary`,
        because a metrics system that silently loses data is worse than one
        that says it lost some."""
        return self.handler.dropped

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until every queued span has been committed. For tests and for
        a CLI that wants its run on disk before it prints the total."""
        return self.handler.flush_queue(timeout)

    # -- reads, on the caller's connection --------------------------------

    def summary(self, conn: sqlite3.Connection, *, window: str = "24h") -> dict[str, Any]:
        """The tiles and meters for one window. Live counts are the route's to
        add: they come from `JobRunner`, not from a table."""
        payload = queries.summary(conn, window=window)
        payload["spans"] = {"written": self.handler.written, "dropped": self.dropped}
        return payload

    def timeseries(
        self, conn: sqlite3.Connection, *, window: str = "7d", bucket: str | None = None
    ) -> list[dict[str, Any]]:
        """The same numbers per bucket, oldest first, empty buckets included."""
        return queries.timeseries(conn, window=window, bucket=bucket)

    def runs(self, conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
        """The global runs table, newest first, each row carrying its trace id."""
        return queries.runs(conn, limit=limit)

    def spans(self, conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
        """One run's spans as a tree, for the waterfall."""
        return queries.spans(conn, run_id)

    def prune(self, conn: sqlite3.Connection, before: str) -> int:
        """Delete spans older than an ISO-8601 timestamp. Returns how many.

        There is no retention policy for the demo and this has never been run
        in anger; it exists so that "what happens when the table grows" has an
        answer that is not "nobody thought about it". `analyses` is untouched
        -- the reports are the deliverable.
        """
        with conn:
            cursor = conn.execute("DELETE FROM spans WHERE ts < ?", (before,))
        log.info("metrics.pruned", extra={"spans": cursor.rowcount, "before": before})
        return cursor.rowcount


__all__ = ["METRICS_SQL", "MetricsStore"]
