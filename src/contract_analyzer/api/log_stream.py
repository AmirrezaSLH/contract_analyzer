"""The live console: every project log line, as a fan-out.

The metrics store already hangs a handler off `logger.py` to file `span.end`
records. This is the same seam, pointed at the browser: a handler formats each
record the way stderr does and `publish`es it onto the `Broadcast` that
`GET /api/logs/events` subscribes to.

**It never blocks a run.** `Broadcast.publish` writes with `put_nowait` and
drops the oldest event when a subscriber is behind, which is the same
discipline the analysis stream uses -- a forgotten log tab must not stall a
criterion thread.

The stream does not close while the process lives. A late subscriber gets the
replay buffer and then the live lines, so opening the tab mid-analysis is not
a blank screen.

API lines come from the project logger. MCP is a second process, so those
lines are followed from `.run/mcp.log` -- the same file `start.bash` tails --
and tagged `mcp` so the tab can draw the same prefix column the terminal does.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from pathlib import Path

from ..logger import ConsoleFormatter, Handler, LogRecord, attach_handler, detach_handler
from .sse import Broadcast, Event

#: Lines a late tab still sees. One analysis is tens of lines, not thousands.
BUFFER = 500
#: Where `start.bash` writes the connector's stdout/stderr.
MCP_LOG = Path(".run/mcp.log")


class LogStream:
    """One per process. Built in the API lifespan, next to the metrics store."""

    def __init__(self, buffer: int = BUFFER, *, mcp_log: Path | None = None) -> None:
        self._broadcast = Broadcast(buffer=buffer)
        self._handler = _Handler(self._broadcast)
        self._mcp_log = mcp_log
        self._stop = threading.Event()
        self._tailer: threading.Thread | None = None
        self._installed = False

    def start(self) -> LogStream:
        """Attach to the project logger, and follow the MCP log if given one."""
        if not self._installed:
            self._handler.setFormatter(ConsoleFormatter())
            attach_handler(self._handler)
            if self._mcp_log is not None:
                self._tailer = threading.Thread(
                    target=self._tail_mcp, name="log-mcp-tail", daemon=True
                )
                self._tailer.start()
            self._installed = True
        return self

    def close(self) -> None:
        """Detach. Open subscribers see end-of-stream. Idempotent."""
        self._stop.set()
        if self._installed:
            detach_handler(self._handler)
            self._installed = False
        self._broadcast.close("end")

    def subscribe(self) -> Iterator[Event]:
        return self._broadcast.subscribe()

    def _tail_mcp(self) -> None:
        """`tail -f` of the connector log. Waits for the file if MCP is late."""
        path = self._mcp_log
        if path is None:
            return
        while not self._stop.is_set():
            try:
                with path.open(encoding="utf-8", errors="replace") as fh:
                    fh.seek(0, os.SEEK_END)
                    while not self._stop.is_set():
                        line = fh.readline()
                        if line:
                            text = line.rstrip("\n\r")
                            if text:
                                self._broadcast.publish(
                                    "log",
                                    {"line": text, "level": "INFO", "source": "mcp"},
                                )
                            continue
                        self._stop.wait(0.25)
                        try:
                            if path.stat().st_size < fh.tell():
                                fh.seek(0)
                        except OSError:
                            break
            except FileNotFoundError:
                self._stop.wait(0.5)
            except OSError:
                self._stop.wait(0.5)


class _Handler(Handler):
    """Format a record and publish. Never raises, never blocks, never logs."""

    def __init__(self, broadcast: Broadcast) -> None:
        super().__init__()
        self._broadcast = broadcast

    def emit(self, record: LogRecord) -> None:
        try:
            line = self.format(record)
            self._broadcast.publish(
                "log", {"line": line, "level": record.levelname, "source": "api"}
            )
        except Exception:  # noqa: BLE001 - a bad record must not fail the run
            pass


__all__ = ["BUFFER", "MCP_LOG", "LogStream"]
