"""What a handler is given, and what it must never take.

Three shared things live on `app.state` for the life of the process -- the
settings, the embedder and the job runner -- because building any of them per
request is either wasteful (the embedder holds an HTTP client; the local one
holds a model) or wrong (a second job runner would be a second pool).

Connections do not. `get_conn` opens one per request and closes it after,
because a request is the natural unit and SQLite connections are not safely
shared between threads.

It opens with `same_thread=False`, which is the case `db.py` was written for and
says so: Starlette runs a sync dependency in a worker thread and an `async`
endpoint on the event loop, so the connection is *created* on one thread and
*used* on another. That is not concurrent use -- the dependency finishes before
the handler starts, and the handler finishes before the teardown runs -- but
sqlite3's default check cannot tell the difference and refuses it. Handing one
connection to two threads at once is still a bug; this flag only stops sqlite3
from catching a bug we do not have.

**A streaming response must not use `get_conn` at all.** Whether a dependency's
teardown runs before or after a streaming body is consumed has changed across
FastAPI releases, and a connection closed under a half-finished chat is a very
confusing failure. `/api/chat` and `/api/analyses/{id}/events` open their own connection
inside the generator and close it there, where the lifetime is the stream's by
construction rather than by framework ordering.

`require_key` is the whole of authentication: a static `X-API-Key` compared in
constant time, and unset means open, which is the local demo. Production would
be OAuth 2.1 with `document_id` scoped per tenant -- see docs/api.md, which says
so rather than leaving a reader to assume this is the intended end state.
"""

from __future__ import annotations

import hmac
import sqlite3
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader

from ..config import Settings
from ..db import get_db
from ..embeddings.base import Embedder
from .errors import unauthorized
from .jobs import JobRunner


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_embedder(request: Request) -> Embedder | None:
    """The process-wide embedder, or None when the provider could not be built.

    None rather than an exception: retrieval in `keyword` mode needs no
    embedder, so an API with no embedding key still answers questions -- it just
    cannot ingest, and `POST /documents` is where that is reported.
    """
    return request.app.state.embedder


def get_runner(request: Request) -> JobRunner:
    return request.app.state.runner


def get_client(request: Request) -> Any:
    """The Anthropic client, or None when there is no key. Routes that need one
    raise `no_api_key()` rather than letting the job fail later."""
    return request.app.state.client


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """One connection per request, closed after. Never for a streaming body."""
    conn = get_db(request.app.state.settings, same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


#: `APIKeyHeader` rather than a plain `Header` for one reason: FastAPI puts it
#: in the OpenAPI document as a security scheme, and that document *is* the
#: connector deliverable. `auto_error=False` so a missing header reaches
#: `require_key`, which answers in this API's error envelope instead of
#: FastAPI's.
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_key(
    request: Request,
    key: Annotated[str | None, Depends(api_key_header)] = None,
) -> None:
    """Reject the request unless it carries the configured key.

    `compare_digest`, not `==`: the comparison is short and remote, so a timing
    attack on it is far-fetched -- but a constant-time compare costs nothing and
    removes the need to argue about it.
    """
    expected = request.app.state.settings.api_key_value
    if not expected:
        return
    if not key or not hmac.compare_digest(key, expected):
        raise unauthorized()


SettingsDep = Annotated[Settings, Depends(get_settings)]
ConnDep = Annotated[sqlite3.Connection, Depends(get_conn)]
EmbedderDep = Annotated[Embedder | None, Depends(get_embedder)]
RunnerDep = Annotated[JobRunner, Depends(get_runner)]
ClientDep = Annotated[Any, Depends(get_client)]
Protected = Depends(require_key)

__all__ = [
    "ClientDep",
    "api_key_header",
    "ConnDep",
    "EmbedderDep",
    "Protected",
    "RunnerDep",
    "SettingsDep",
    "get_client",
    "get_conn",
    "get_embedder",
    "get_runner",
    "get_settings",
    "require_key",
]
