"""`span.end` log records into `spans` rows, without touching a single emitter.

This is the whole trick of phase 2, and its consequences are the argument for
it:

* **The eight emitting modules change by zero lines.** `generation/`,
  `retrieval/`, `ingest/` and `compliance/` already wrap their work in
  `span()`, and none of them gains a `record_span()` call -- nor does any
  module written after this one have to remember one.
* **The CLI is instrumented for free.** `scripts/analyze.py` calls the same
  `configure_logging` the API does, so `make analyze` populates the same table.
  A KPI page that only sees HTTP traffic measures the surface, not the system.
* **The log file and the table cannot disagree**, because they are the same
  records. `.run/app.jsonl` stays the source of truth for a walkthrough; the
  table becomes the source of truth for an aggregate.

**Telemetry must never hold up an analysis.** That is not a preference, it is
the constraint the whole design is shaped by:

* `emit()` pushes onto a **bounded** queue and **drops on overflow**. It never
  blocks a criterion thread in order to record that a criterion thread was
  busy.
* A drop increments a counter that `GET /metrics/summary` reports. A metrics
  system that silently loses data is worse than one that says it lost some.
* One **daemon writer thread** drains the queue and writes in batches, on a
  connection of its own. A request's connection is not available to it and
  must not be.
* **`emit()` never raises.** Building the row and putting it on the queue are
  each wrapped: a malformed span attribute must not fail the run it describes.
* **Every span is stored, no sampling.**

`logging.Handler` and `LogRecord` are imported from `logger.py` rather than
from `logging`, because exactly one module in this package is allowed to
import `logging` and it is that one (`tests/test_logger.py` enforces it). The
dependency runs `metrics -> logger` and never the other way.
"""

from __future__ import annotations

import contextlib
import json
import queue
import sqlite3
import threading
import time
from datetime import UTC, datetime
from typing import Any

from ..logger import Handler, LogRecord, get_logger

log = get_logger(__name__)

#: The message `logger.span()` logs when a block finishes. `span.start` is
#: ignored: a row per start would double the table to record nothing an end
#: does not already carry, including the latency.
SPAN_END = "span.end"

#: Columns in the order the INSERT binds them.
COLUMNS = (
    "span_id", "parent_span_id", "trace_id", "run_id", "name", "status",
    "latency_ms", "ts", "surface", "criterion", "document_id", "model",
    "input_tokens", "output_tokens", "cost_usd", "attrs",
)

#: Attributes promoted out of the JSON bag into columns of their own. Every
#: KPI query touches these; the rest stay in `attrs`.
_PROMOTED = ("surface", "criterion", "document_id", "model", "input_tokens",
             "output_tokens", "cost_usd")

_INSERT = (
    f"INSERT OR REPLACE INTO spans ({', '.join(COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(COLUMNS))})"
)

#: Rows the queue holds before it starts dropping. A five-criterion analysis
#: produces roughly seventy; ten thousand is two orders of magnitude of head
#: room over anything a demo can generate, and about a megabyte of tuples.
CAPACITY = 10_000
#: Rows per transaction. Large enough that a criterion's spans land in one
#: commit, small enough that a quiet process is never holding much.
BATCH = 128
#: How long the writer waits for a first row before looping. Also the longest
#: a shutdown waits to notice its sentinel.
POLL_SECONDS = 0.2


class SpanHandler(Handler):
    """A logging handler that files `span.end` records as rows.

    Built with a callable that opens the writer's connection rather than with
    a connection, because the connection must be created on the thread that
    uses it and `start()` is what starts that thread.
    """

    def __init__(self, connect: Any, *, capacity: int = CAPACITY, batch: int = BATCH) -> None:
        super().__init__()
        self._connect = connect
        self._batch = batch
        self._queue: queue.Queue[tuple | None] = queue.Queue(maxsize=capacity)
        self._dropped = 0
        self._written = 0
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    # -- the emitting side, on whatever thread logged --------------------

    def emit(self, record: LogRecord) -> None:
        """File one row. Never raises, never blocks, never samples.

        The `msg` comparison is first and is the cheap path: this handler sits
        on the project's root logger and sees every line the process logs, of
        which the overwhelming majority are not spans.
        """
        if record.msg != SPAN_END:
            return
        try:
            row = _row(record)
        except Exception:  # noqa: BLE001 - a bad attribute must not fail the run
            self._dropped += 1
            return
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            # Dropped rather than blocked, and counted rather than hidden.
            self._dropped += 1

    # -- the writing side, on one daemon thread ---------------------------

    def start(self) -> SpanHandler:
        """Open the writer's connection, apply nothing, and start draining."""
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._drain, name="metrics-writer", daemon=True
        )
        self._thread.start()
        return self

    def _drain(self) -> None:
        conn = self._connect()
        try:
            while not self._stopping.is_set():
                batch = self._take()
                if batch:
                    self._write(conn, batch)
        finally:
            conn.close()

    def _take(self) -> list[tuple]:
        """Up to `batch` rows. Sets `_stopping` if the sentinel is among them.

        Blocks on the first row with a timeout, then takes whatever else is
        already there without waiting -- so a busy run commits in batches and
        a quiet process still files its one span within a fifth of a second.
        Rows queued *ahead* of the sentinel are returned and written; `close`
        drains before sending it, so nothing is queued behind it.
        """
        batch: list[tuple] = []
        while len(batch) < self._batch:
            try:
                # Wait for the first row; take the rest only if they are
                # already there. A busy run commits in batches, a quiet one
                # still files its single span within POLL_SECONDS.
                row = (
                    self._queue.get(timeout=POLL_SECONDS)
                    if not batch
                    else self._queue.get_nowait()
                )
            except queue.Empty:
                break
            if row is None:
                self._queue.task_done()
                self._stopping.set()
                break
            batch.append(row)
        return batch

    def _write(self, conn: sqlite3.Connection, batch: list[tuple]) -> None:
        try:
            with conn:
                conn.executemany(_INSERT, batch)
            self._written += len(batch)
        except Exception as exc:  # noqa: BLE001 - the run does not care
            self._dropped += len(batch)
            # Safe to log: this is not a span record, so it cannot come back
            # round through `emit` as another row.
            log.warning("metrics.write_failed", extra={"rows": len(batch), "error": str(exc)})
        finally:
            for _ in batch:
                self._queue.task_done()

    # -- what the store and the summary ask it ----------------------------

    @property
    def dropped(self) -> int:
        """Rows this handler threw away: a full queue, a malformed record, or
        a failed write. `GET /metrics/summary` reports it."""
        return self._dropped

    @property
    def written(self) -> int:
        return self._written

    def flush_queue(self, timeout: float = 5.0) -> bool:
        """Wait until everything queued has been committed. True if it was.

        `Queue.join()` with a deadline -- the plain one cannot time out, and a
        test that hangs forever because a writer died is worse than one that
        fails.
        """
        deadline = time.monotonic() + timeout
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._queue.all_tasks_done.wait(remaining)
        return True

    def close(self) -> None:
        """Drain what is queued, stop the writer, close its connection.

        Best effort by design: the thread is a daemon, so a process that exits
        without calling this loses at most one batch of telemetry and never
        hangs on it.
        """
        thread, self._thread = self._thread, None
        if thread is not None:
            self.flush_queue(timeout=2.0)
            # The flush above emptied it; suppressed anyway, because a
            # shutdown that raises is a shutdown that leaves a thread behind.
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(None)
            thread.join(timeout=2.0)
        super().close()


def _row(record: LogRecord) -> tuple:
    """One `span.end` record as the tuple `_INSERT` binds.

    Every field is read defensively. This runs on a criterion thread inside a
    run that is already a minute long and a dollar deep, and the one outcome
    that is not acceptable is an exception coming out of here.
    """
    attrs = {
        key: value
        for key, value in record.__dict__.items()
        if key not in _RECORD_FIELDS and not key.startswith("_")
    }
    name = str(attrs.pop("span", record.name))
    status = attrs.pop("status", None)
    latency_ms = _number(attrs.pop("latency_ms", None))
    promoted = {key: attrs.pop(key, None) for key in _PROMOTED}
    return (
        getattr(record, "span_id", None),
        getattr(record, "parent_span_id", None),
        getattr(record, "trace_id", None),
        getattr(record, "run_id", None),
        name,
        None if status is None else str(status),
        latency_ms,
        datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
        _text(promoted["surface"]),
        _text(promoted["criterion"]),
        _integer(promoted["document_id"]),
        _text(promoted["model"]),
        _integer(promoted["input_tokens"]),
        _integer(promoted["output_tokens"]),
        _number(promoted["cost_usd"]),
        _json(attrs),
    )


#: LogRecord's own attributes, plus the context ids read by name above. What
#: is left over is the span's attribute bag.
_RECORD_FIELDS = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
        "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
        "relativeCreated", "thread", "threadName", "processName", "process", "message",
        "taskName", "asctime", "trace_id", "span_id", "parent_span_id", "run_id",
    }
)


def _text(value: Any) -> str | None:
    """`str(value)`, and `None` for an object whose `__str__` raises. Nothing
    a span carries is worth failing a row over, let alone a run."""
    if value is None:
        return None
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - a promoted column is not worth a row
        return None


def _integer(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


#: What a value that could not be serialised is replaced with. Visible in the
#: waterfall, which is the point: the span is still there and it says which
#: attribute was the problem.
UNSERIALISABLE = "<unserialisable>"


def _json(attrs: dict[str, Any]) -> str | None:
    """The leftover bag as JSON. `default=str` handles anything unusual.

    `str` itself can raise -- an object with a broken `__repr__` is rare and
    entirely possible -- so the whole bag is retried key by key rather than
    thrown away. One bad attribute costs that attribute, not the other
    attributes and not the row: **every span is stored** is a claim this
    module makes, and dropping a row over a `__repr__` would falsify it.
    """
    if not attrs:
        return None
    try:
        return json.dumps(attrs, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001 - retried per key below
        pass
    safe: dict[str, Any] = {}
    for key, value in attrs.items():
        try:
            json.dumps(value, default=str)
        except Exception:  # noqa: BLE001 - this one value, named as such
            safe[key] = UNSERIALISABLE
        else:
            safe[key] = value
    try:
        return json.dumps(safe, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001 - a bag is never worth a row
        return None


__all__ = ["BATCH", "CAPACITY", "COLUMNS", "SPAN_END", "UNSERIALISABLE", "SpanHandler"]
