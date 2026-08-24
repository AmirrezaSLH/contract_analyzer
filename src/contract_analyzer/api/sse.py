"""Server-sent events: the framing, and the fan-out behind it.

`queue.Queue` is a single-consumer structure, and both of this API's streams
have more than one plausible consumer -- a UI that reconnects, a second tab, an
MCP client watching a job the UI started. So a `Broadcast` is not one queue but
a list of them, one per subscriber, plus a replay buffer:

* **`publish` never blocks.** Each subscriber's queue is bounded and written
  with `put_nowait`; on overflow the oldest event is dropped. A criterion thread
  must not be held up because someone opened a stream and stopped reading it.
* **A late subscriber is not a lost one.** `subscribe` copies the replay buffer
  into the new queue before the queue starts receiving, so a client that
  connects at second 40 gets what it missed and then the live events, in order
  and without duplicates.
* **A finished stream ends rather than hangs.** `close` records the terminal
  event; a client subscribing afterwards receives the replay, the terminal
  event, and end-of-stream. Nothing waits for a keepalive to time out.

Ordering is what the lock buys. `publish` appends to the replay buffer and
writes to every queue inside one critical section, and `subscribe` copies the
buffer and registers inside the same one, so no event can slip between the copy
and the registration -- the two ways a fan-out normally loses or duplicates one.
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

#: Sent when a subscriber's queue overflowed, so a client can tell "nothing
#: happened" from "you were too slow and some of it is gone".
DROPPED = "dropped"


@dataclass(frozen=True)
class Event:
    """One SSE frame: a named event with a JSON payload."""

    name: str
    data: dict[str, Any]

    @property
    def json(self) -> str:
        """The `data:` line's contents.

        Encoded here rather than left to the SSE library: `sse-starlette` calls
        `str()` on whatever it is given, and `str(dict)` is a Python repr with
        single quotes -- valid to look at, not parseable by any client.
        """
        return json.dumps(self.data, default=str)


class Broadcast:
    """A fan-out with replay. One per job, one per chat stream."""

    def __init__(self, buffer: int = 256) -> None:
        self._buffer = buffer
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[Event | None]] = []
        self._replay: list[Event] = []
        self._closed = False

    def publish(self, name: str, data: dict[str, Any] | None = None) -> None:
        """Deliver to every subscriber, and remember for the late ones.

        The payload is a dict rather than `**kwargs`: a `tool_call` event has a
        field called `name`, and a signature that takes both the event name and
        the payload as keywords collides on it. That is not hypothetical -- it
        was a `TypeError` on the worker thread that failed the whole job.
        """
        event = Event(name, data or {})
        with self._lock:
            if self._closed:
                return
            self._replay.append(event)
            del self._replay[: max(0, len(self._replay) - self._buffer)]
            for sink in self._subscribers:
                _offer(sink, event)

    def close(self, name: str = "done", data: dict[str, Any] | None = None) -> None:
        """Publish the terminal event and end every stream, live or future."""
        with self._lock:
            if self._closed:
                return
            event = Event(name, data or {})
            self._replay.append(event)
            del self._replay[: max(0, len(self._replay) - self._buffer)]
            self._closed = True
            for sink in self._subscribers:
                _offer(sink, event)
                _terminate(sink)
            self._subscribers.clear()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def subscribe(self) -> Iterator[Event]:
        """Every event so far, then every event until the stream closes.

        The generator deregisters itself in a `finally`, so a client that hangs
        up mid-analysis does not leave a queue filling behind it.
        """
        sink: queue.Queue[Event | None] = queue.Queue(maxsize=self._buffer)
        with self._lock:
            backlog = list(self._replay)
            if self._closed:
                yield from backlog
                return
            self._subscribers.append(sink)
        try:
            yield from backlog
            while True:
                event = sink.get()
                if event is None:
                    return
                yield event
        finally:
            with self._lock:
                if sink in self._subscribers:
                    self._subscribers.remove(sink)


def _offer(sink: queue.Queue[Event | None], event: Event) -> None:
    """Write without blocking, dropping the oldest event if the reader is behind.

    A subscriber that has stopped reading is a client's problem. Blocking here
    would make it the analysis's problem, which is the one outcome that is not
    acceptable: a criterion thread waiting on a dead browser tab.
    """
    try:
        sink.put_nowait(event)
        return
    except queue.Full:
        pass
    try:
        sink.get_nowait()
        sink.put_nowait(Event(DROPPED, {"reason": "subscriber too slow"}))
        sink.put_nowait(event)
    except (queue.Empty, queue.Full):  # pragma: no cover - racing another reader
        pass


def _terminate(sink: queue.Queue[Event | None]) -> None:
    """Push end-of-stream, making room for it if the reader is behind.

    The sentinel is the one item that must never be dropped: a subscriber that
    misses it waits on `get()` forever, and the request holding that generator
    never completes.
    """
    while True:
        try:
            sink.put_nowait(None)
            return
        except queue.Full:
            try:
                sink.get_nowait()
            except queue.Empty:  # pragma: no cover - a reader drained it first
                return


__all__ = ["DROPPED", "Broadcast", "Event"]
