"""The one logging surface for the project.

Every module does::

    from ..logger import get_logger
    log = get_logger(__name__)

and nothing else imports :mod:`logging` directly (``tests/test_logger.py``
enforces that). Centralising it buys three things the assignment asks for:

* **Structured output.** The file handler writes one JSON object per line with
  the fields an observability platform would index: timestamp, level, logger,
  message, and whatever ``extra={...}`` the caller attached.
* **Trace correlation.** ``trace_id`` / ``span_id`` / ``parent_span_id`` are
  carried in :mod:`contextvars`, so a request sets them once and every log
  line beneath it -- parser, retriever, HTTP retries, LLM calls -- carries
  them without threading arguments through every signature.
* **Extras.** ``log.info("parsed", extra={"pages": 21})`` is the idiom.
  Python reserves a few names (``filename``, ``module``, ``name``, ``msg``)
  for the record itself -- use ``file``/``path`` instead of ``filename``.
* **Spans.** :func:`span` is a context manager that logs ``span.start`` and
  ``span.end`` with the elapsed time and status. It is the seam the later
  metrics store hangs off; in this phase it only logs.

The console handler prints a compact human line (``12:01:03 INFO parse.pdf
parsed 21 pages trace=ab12``) because a demo audience reads the terminal, and
the JSON file exists for grep/jq and for the walkthrough.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# --- context ----------------------------------------------------------------

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)
_span_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("span_id", default=None)
_parent_span_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "parent_span_id", default=None
)

#: Attributes of a LogRecord that are not user ``extra`` fields.
_RESERVED = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
        "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
        "relativeCreated", "thread", "threadName", "processName", "process", "message",
        "taskName", "asctime", "trace_id", "span_id", "parent_span_id",
    }
)


def new_id(length: int = 16) -> str:
    """A random hex id. 16 hex chars for a span, 32 for a trace, by convention."""
    return uuid.uuid4().hex[:length]


def current_trace_id() -> str | None:
    return _trace_id.get()


def current_span_id() -> str | None:
    return _span_id.get()


@contextmanager
def trace_context(trace_id: str | None = None) -> Iterator[str]:
    """Bind a trace id for everything logged inside the block.

    A fresh id is minted when none is given. Nested calls with no id inherit
    the outer trace rather than starting a new one.
    """
    tid = trace_id or _trace_id.get() or new_id(32)
    token = _trace_id.set(tid)
    try:
        yield tid
    finally:
        _trace_id.reset(token)


@contextmanager
def span(name: str, logger: logging.Logger | None = None, **attrs: Any) -> Iterator[dict[str, Any]]:
    """Log ``span.start`` / ``span.end`` around a block, with timing and status.

    The yielded dict is the span's attribute bag: callers add fields to it
    (``s["chunks"] = 42``) and they appear on the ``span.end`` line. An
    exception inside the block is logged as ``status="error"`` with the
    exception type and re-raised.
    """
    log = logger or get_logger("span")
    sid = new_id()
    parent_token = _parent_span_id.set(_span_id.get())
    span_token = _span_id.set(sid)
    bag: dict[str, Any] = dict(attrs)
    started = time.perf_counter()
    log.info("span.start", extra={"span": name, **bag})
    try:
        yield bag
    except BaseException as exc:
        bag["status"] = "error"
        bag["error"] = f"{type(exc).__name__}: {exc}"
        raise
    else:
        bag.setdefault("status", "ok")
    finally:
        bag["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        log.info("span.end", extra={"span": name, **bag})
        _span_id.reset(span_token)
        _parent_span_id.reset(parent_token)


# --- formatters -------------------------------------------------------------


class _ContextFilter(logging.Filter):
    """Stamp the context ids onto every record before any handler sees it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = _trace_id.get()
        record.span_id = _span_id.get()
        record.parent_span_id = _parent_span_id.get()
        return True


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {
        k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")
    }


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": getattr(record, "trace_id", None),
            "span_id": getattr(record, "span_id", None),
            "parent_span_id": getattr(record, "parent_span_id", None),
        }
        payload.update(_extras(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """``HH:MM:SS LEVEL logger message key=value ... trace=abcd``"""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        name = record.name.removeprefix("contract_analyzer.")
        fields = " ".join(f"{k}={_short(v)}" for k, v in _extras(record).items())
        trace = getattr(record, "trace_id", None)
        tail = f" trace={trace[:8]}" if trace else ""
        line = f"{ts} {record.levelname:<5} {name} {record.getMessage()}"
        if fields:
            line += f" {fields}"
        line += tail
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def _short(value: Any, limit: int = 80) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- configuration ----------------------------------------------------------

_ROOT = "contract_analyzer"
_configured = False


def configure_logging(
    level: str | int = "INFO",
    json_file: str | Path | None = None,
    *,
    console: bool = True,
    force: bool = False,
) -> None:
    """Install the console and JSON handlers on the project's root logger.

    Idempotent: the second call is a no-op unless ``force`` is set, so a CLI
    script and the library it imports cannot double-print. Third-party
    loggers (httpx, anthropic, openai) are raised to WARNING so the JSON file
    only carries our own lines and our own HTTP retry entries.
    """
    global _configured
    root = logging.getLogger(_ROOT)
    if _configured and not force:
        return
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(level)
    root.propagate = False
    # On the handlers, not the logger: a logger's filters only see records
    # logged to it directly, and every record here arrives by propagation.
    context = _ContextFilter()

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(ConsoleFormatter())
        stream.addFilter(context)
        root.addHandler(stream)
    if json_file:
        path = Path(json_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        file_handler.addFilter(context)
        root.addHandler(file_handler)

    for noisy in ("httpx", "httpcore", "anthropic", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """The project logger for ``name``, always under the ``contract_analyzer`` root."""
    if name == _ROOT or name.startswith(_ROOT + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_ROOT}.{name}")
