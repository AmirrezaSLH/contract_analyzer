"""Ask a question about one contract, cited, streamed.

The one interaction where latency is felt token by token, so it streams by
default. `stream=false` returns the same answer as one JSON body, for a caller
that cannot consume SSE -- an MCP tool, a connector, a test.

Two things about how this is wired, both of which look like accidents until
they are explained:

**The generator owns its connection.** `chat()` is synchronous and blocks on the
model, so it runs on a worker thread and pushes tokens into a queue the SSE
generator drains. It does *not* take the per-request `get_conn` dependency:
whether a dependency's teardown runs before or after a streaming body is
consumed has changed across FastAPI releases, and a closed connection under a
half-finished chat is a very confusing failure. The connection is opened where
it is used and closed when the stream ends, so its lifetime is the stream's by
construction.

**History belongs to the client.** This API keeps no transcript. The UI holds
its session state, the MCP server passes what the model gave it, and `chat()`
replays the last eight messages as plain text. Statelessness is what lets four
consumers share one backend without a session store between them.
"""

from __future__ import annotations

import queue
import threading
from typing import Any

from fastapi import APIRouter, Request, status
from sse_starlette.sse import EventSourceResponse

from ...db import get_db
from ...documents import get_document
from ...generation.chat import chat
from ...http_client import HttpFailure
from ...logger import current_trace_id, get_logger, trace_context
from ..deps import ClientDep, ConnDep, EmbedderDep, Protected, SettingsDep
from ..errors import ApiError, document_not_found, no_api_key
from ..schemas import Answer, ChatRequest, answer_of, as_dict
from ..sse import Event

log = get_logger(__name__)

router = APIRouter(tags=["chat"], dependencies=[Protected])

#: Pushed by the worker thread when it is finished, whatever the outcome.
_END = object()


@router.post(
    "/chat",
    summary="A cited answer about one contract",
    description=(
        "Streams server-sent events by default: `text` deltas, a `tool_call` for each "
        "search the model makes, one `citations` event, then `done` with usage and cost. "
        "With `stream=false` the same answer comes back as one JSON body. Quotes are "
        "extracted by the model API from the passages we sent, so they cannot be invented."
    ),
    response_model=Answer,
    responses={status.HTTP_200_OK: {"content": {"text/event-stream": {}}}},
)
def ask(
    body: ChatRequest,
    request: Request,
    conn: ConnDep,
    settings: SettingsDep,
    embedder: EmbedderDep,
    client: ClientDep,
):
    # Validated on the request's own connection, before any streaming starts:
    # a 404 must be a 404, not an `error` event inside a 200.
    if get_document(conn, body.document_id) is None:
        raise document_not_found(body.document_id)
    if client is None:
        raise no_api_key()
    require_allowed_model(body.model, settings)

    history = [m.model_dump() for m in body.history]
    if not body.stream:
        result = chat(
            body.question, conn, embedder, settings,
            document_id=body.document_id, history=history, client=client,
            model=body.model, retrieval_mode=body.retrieval_mode, top_k=body.top_k,
        )
        return answer_of(result)

    return EventSourceResponse(
        _stream(body, settings, embedder, client, history, current_trace_id()),
        ping=int(settings.api_keepalive_seconds),
        headers={"X-Trace-Id": current_trace_id() or ""},
    )


def require_allowed_model(model: str | None, settings) -> None:
    """Refuse a model this deployment does not offer.

    An allowlist rather than free text, and checked here rather than in the
    pydantic model, which has no way to reach settings. This endpoint is open
    when `API_KEY` is unset, so an unvalidated `model` is an invitation to
    spend this deployment's key on whatever the caller names. `chat_models` is
    published by `GET /health` so a client can render exactly the choices that
    will be accepted.
    """
    if model is None or model in settings.chat_models:
        return
    raise ApiError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "validation",
        f"model: {model!r} is not offered by this deployment.",
        "Pick one of the models GET /api/health lists as chat_models, or omit the field "
        "to use the configured default.",
    )


def _stream(body, settings, embedder, client, history, trace_id):
    """Run `chat()` on a worker thread and yield what it produces.

    The queue is unbounded on purpose, unlike the job fan-out's: this producer
    has exactly one consumer, the generator below, and it is drained as fast as
    the socket accepts. There is no slow-subscriber case to defend against.
    """
    sink: queue.Queue[Any] = queue.Queue()
    outcome: dict[str, Any] = {}

    def work() -> None:
        with trace_context(trace_id):
            conn = get_db(settings, same_thread=False)
            try:
                outcome["result"] = chat(
                    body.question, conn, embedder, settings,
                    document_id=body.document_id, history=history, client=client,
                    model=body.model, retrieval_mode=body.retrieval_mode, top_k=body.top_k,
                    on_text=lambda text: sink.put(Event("text", {"text": text})),
                    on_event=lambda event: sink.put(_agent_event(event)),
                )
            except HttpFailure as exc:
                outcome["error"] = ("upstream_failure", str(exc))
            except Exception as exc:  # noqa: BLE001 - the stream must always end
                log.exception("api.chat.failed")
                outcome["error"] = (type(exc).__name__, str(exc))
            finally:
                conn.close()
                sink.put(_END)

    worker = threading.Thread(target=work, name="chat", daemon=True)
    worker.start()

    while (item := sink.get()) is not _END:
        if item is not None:
            yield {"event": item.name, "data": item.json}

    # Joined before the terminal event: the answer object is built on that
    # thread, and a client that sees `done` must be able to trust the totals.
    worker.join()
    if "error" in outcome:
        code, message = outcome["error"]
        yield {"event": "error", "data": Event("error", {"code": code, "message": message}).json}
        return
    answer = answer_of(outcome["result"])
    yield {"event": "citations", "data": Event("citations", as_dict(answer)).json}
    yield {
        "event": "done",
        "data": Event(
            "done",
            {
                "usage": answer.usage,
                "cost_usd": answer.cost_usd,
                # The model that answered, not the one that was asked for: a
                # usage line that reports the request rather than the run is
                # the wrong half of the story.
                "model": answer.model,
                "stop_reason": answer.stop_reason,
                "ended_by": answer.ended_by,
                "tool_calls": answer.tool_calls,
                "grounded": answer.grounded,
            },
        ).json,
    }


def _agent_event(event: dict[str, Any]) -> Event | None:
    """The agent's events, filtered to what a client can use.

    `text` from the loop is the model's preamble between tool calls -- "let me
    check the exhibits" -- which belongs in the transcript no more than the
    tool call itself does, so it is dropped and only the finisher's streamed
    answer becomes `text`.
    """
    if event.get("type") != "tool_call":
        return None
    return Event(
        "tool_call",
        {
            "name": event.get("name"),
            "args": event.get("args"),
            "returned": event.get("returned"),
            "new": event.get("new"),
            "error": event.get("error"),
        },
    )
