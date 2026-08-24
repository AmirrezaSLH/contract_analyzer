"""The one place this UI talks to the API.

Every request leaves through here, which buys four things that would otherwise
be scattered across four view modules:

* **One error type.** The API answers every failure as
  ``{"error": {"code", "message", "hint"}}``. That envelope becomes `ApiError`,
  whose ``code`` is what a view branches on and whose ``hint`` is written for a
  person -- it is the second line of every error surface.
* **One trace id per user action.** `X-Trace-Id` is minted when a user does
  something (an upload, a submission, a question), not per request: one action
  that costs three calls is one id in ``.run/app.jsonl``, which is what makes
  the log walkthrough possible. See `trace_for`.
* **One place the key is attached**, so a deployment that sets ``API_KEY`` does
  not need every view to remember it.
* **One SSE reader.** `chat_stream` is a dozen lines of line-splitting over
  ``client.stream``; the alternative is a second HTTP stack for the sake of a
  parser this small.

`httpx2` rather than `requests`: it is already a core dependency, because both
model SDKs run on it.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx2 as httpx

#: Long enough for an upload to be parsed, chunked and embedded before the
#: connection is given up on -- that is seconds of blocking work in the API's
#: threadpool, and it is the one request here that is not fast.
UPLOAD_TIMEOUT = 300.0
#: A chat answer streams, so the read timeout is per chunk rather than for the
#: whole answer. The model can think for a while before the first token.
STREAM_TIMEOUT = 180.0
DEFAULT_TIMEOUT = 30.0


class ApiError(Exception):
    """A failure the API described, or a failure reaching it at all.

    `code` is the stable string to branch on. Two codes are minted here rather
    than by the API, because they describe something that happened on this side
    of the wire: `unreachable` when the connection failed, and `bad_response`
    when what came back was not the envelope. Both carry a hint, because a view
    renders `hint` unconditionally.
    """

    def __init__(
        self,
        code: str,
        message: str,
        hint: str | None = None,
        *,
        status: int | None = None,
        trace_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.status = status
        self.trace_id = trace_id


@dataclass
class StreamBox:
    """What a chat stream produces besides text.

    `st.write_stream` consumes the generator and returns the concatenated
    text, so everything else the stream carries -- the citations, the usage,
    the tool trail -- has to land somewhere the caller still holds. This is
    that somewhere.
    """

    citations: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    #: Set when the stream ended with an `error` event. The partial text is
    #: kept: an answer cut off halfway is still worth reading, and a bare
    #: spinner that never resolves is the worst of the three outcomes.
    error: ApiError | None = None


def new_trace_id() -> str:
    """One id per user action. Short enough to read off a screen and type into
    a grep, which is the whole point of showing it."""
    return uuid.uuid4().hex[:16]


class ApiClient:
    """One method per endpoint. No caching, no retries, no state.

    Retries are deliberately absent: the API's own transport already retries
    everything that leaves *it*, and a UI that retries a failed upload turns
    one 413 into three. A failed request here is shown to the user.
    """

    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("CA_API_URL", "http://localhost:8000")).rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("API_KEY")

    # -- plumbing -----------------------------------------------------------

    def _headers(self, trace_id: str | None = None) -> dict[str, str]:
        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if trace_id:
            headers["X-Trace-Id"] = trace_id
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        trace_id: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        **kwargs: Any,
    ) -> Any:
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.request(
                    method, url, headers=self._headers(trace_id), **kwargs
                )
        except httpx.HTTPError as exc:
            raise ApiError(
                "unreachable",
                f"Could not reach the API at {self.base_url}.",
                "Check that the API is running and that CA_API_URL points at it. "
                "Under Docker that is `make docker-up`.",
                trace_id=trace_id,
            ) from exc
        return self._body(response, trace_id)

    @staticmethod
    def _body(response: httpx.Response, trace_id: str | None = None) -> Any:
        trace_id = response.headers.get("X-Trace-Id") or trace_id
        if response.status_code == 204:
            return None
        try:
            payload = response.json()
        except ValueError:
            # A proxy's HTML error page, or a body that was cut off. The status
            # is the only thing left that means anything.
            if response.is_success:
                raise ApiError(
                    "bad_response",
                    "The API returned something that is not JSON.",
                    "This usually means a proxy answered instead of the API. Check "
                    "CA_API_URL.",
                    status=response.status_code,
                    trace_id=trace_id,
                ) from None
            payload = {}
        if response.is_success:
            return payload
        error = (payload or {}).get("error") or {}
        raise ApiError(
            error.get("code", "http_error"),
            error.get("message", f"The API answered {response.status_code}."),
            error.get("hint"),
            status=response.status_code,
            trace_id=trace_id,
        )

    # -- health and reference ----------------------------------------------

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def criteria(self) -> list[dict[str, Any]]:
        return self._request("GET", "/criteria")

    # -- documents ----------------------------------------------------------

    def upload(self, name: str, data: bytes, *, trace_id: str | None = None) -> dict[str, Any]:
        return self._request(
            "POST",
            "/documents",
            trace_id=trace_id,
            timeout=UPLOAD_TIMEOUT,
            files={"file": (name, data, "application/pdf")},
        )

    def documents(self) -> list[dict[str, Any]]:
        return self._request("GET", "/documents")

    def document(self, document_id: int) -> dict[str, Any]:
        return self._request("GET", f"/documents/{document_id}")

    def sections(self, document_id: int) -> list[dict[str, Any]]:
        return self._request("GET", f"/documents/{document_id}/sections")

    def delete_document(self, document_id: int, *, trace_id: str | None = None) -> None:
        self._request("DELETE", f"/documents/{document_id}", trace_id=trace_id)

    # -- analyses -----------------------------------------------------------

    def create_analysis(
        self,
        document_id: int,
        *,
        criteria: list[str] | None = None,
        trace_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Queue a run, or be handed the one already doing this.

        A `202` and a `200` carrying an in-flight analysis are the same thing
        here: take `analysis_id` from the body either way. The `200` is the
        duplicate-submit guard doing its job -- at roughly a dollar a run, a
        double-clicked button is a real cost -- and treating it as an error
        would be treating a saved dollar as a failure.
        """
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        body: dict[str, Any] = {"document_id": document_id}
        if criteria:
            body["criteria"] = criteria
        return self._request("POST", "/analyses", trace_id=trace_id, json=body, headers=headers)

    def analyses(self, document_id: int) -> list[dict[str, Any]]:
        return self._request("GET", "/analyses", params={"document_id": document_id})

    def analysis(self, analysis_id: str, *, detail: str = "full") -> dict[str, Any]:
        return self._request("GET", f"/analyses/{analysis_id}", params={"detail": detail})

    def cancel_analysis(self, analysis_id: str, *, trace_id: str | None = None) -> dict[str, Any]:
        return self._request("POST", f"/analyses/{analysis_id}/cancel", trace_id=trace_id)

    # -- chat ---------------------------------------------------------------

    def chat(
        self,
        document_id: int,
        question: str,
        *,
        history: list[dict[str, str]] | None = None,
        model: str | None = None,
        retrieval_mode: str | None = None,
        top_k: int | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """One JSON body. The same answer the stream adds up to."""
        return self._request(
            "POST",
            "/chat",
            trace_id=trace_id,
            timeout=STREAM_TIMEOUT,
            json=self._chat_body(
                document_id, question, history, model, retrieval_mode, top_k, stream=False
            ),
        )

    def chat_stream(
        self,
        document_id: int,
        question: str,
        box: StreamBox,
        *,
        history: list[dict[str, str]] | None = None,
        model: str | None = None,
        retrieval_mode: str | None = None,
        top_k: int | None = None,
        trace_id: str | None = None,
    ) -> Iterator[str]:
        """Yield text deltas; put everything else in `box`.

        Shaped for `st.write_stream`, which renders what a generator yields and
        returns the concatenation. Citations arrive once, near the end, and the
        usage arrives with `done` -- neither is text, so neither is yielded.

        A `503` before the stream opens raises, which is what a view wants: no
        turn is appended and the hint is rendered instead. An `error` event
        *inside* the stream cannot raise without discarding the partial answer,
        so it lands in `box.error` and the caller decides.
        """
        body = self._chat_body(
            document_id, question, history, model, retrieval_mode, top_k, stream=True
        )
        try:
            with (
                httpx.Client(timeout=STREAM_TIMEOUT) as client,
                client.stream(
                    "POST",
                    f"{self.base_url}/chat",
                    headers=self._headers(trace_id),
                    json=body,
                ) as response,
            ):
                if not response.is_success:
                    # Read the body before touching it: a streaming response is
                    # not loaded until it is asked for, and `_body` needs the
                    # envelope to build the ApiError this raises.
                    response.read()
                    self._body(response, trace_id)
                yield from self._events(response, box)
        except httpx.HTTPError as exc:
            raise ApiError(
                "unreachable",
                f"The connection to {self.base_url} failed while answering.",
                "Check that the API is still running, then ask again.",
                trace_id=trace_id,
            ) from exc

    @staticmethod
    def _events(response: httpx.Response, box: StreamBox) -> Iterator[str]:
        """SSE is `event:` and `data:` lines separated by blank ones.

        A frame is dispatched when its data line arrives rather than at the
        blank line: every frame this API sends is a single `data:` line, and
        waiting for the separator would hold the last token until the one after
        it. Keepalive comments start with `:` and are ignored.
        """
        name: str | None = None
        for line in response.iter_lines():
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
                continue
            if not line.startswith("data:") or name is None:
                continue
            try:
                data = json.loads(line.split(":", 1)[1].strip())
            except ValueError:
                name = None
                continue
            event, name = name, None
            if event == "text":
                yield data.get("text", "")
            elif event == "tool_call":
                box.tool_calls.append(data)
            elif event == "citations":
                box.citations = data.get("citations", [])
            elif event == "done":
                box.usage = data
            elif event == "error":
                box.error = ApiError(
                    data.get("code", "internal"),
                    data.get("message", "The answer failed partway through."),
                    "The partial answer above is what arrived before it failed. "
                    "Ask again.",
                )
                return

    @staticmethod
    def _chat_body(
        document_id: int,
        question: str,
        history: list[dict[str, str]] | None,
        model: str | None,
        retrieval_mode: str | None,
        top_k: int | None,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        # Omitted rather than sent as null: every one of these is optional on
        # the API and omitting it means "the configured default", which is
        # exactly what a control left alone should mean.
        body: dict[str, Any] = {
            "document_id": document_id,
            "question": question,
            "history": history or [],
            "stream": stream,
        }
        for key, value in (
            ("model", model),
            ("retrieval_mode", retrieval_mode),
            ("top_k", top_k),
        ):
            if value is not None:
                body[key] = value
        return body


__all__ = ["ApiClient", "ApiError", "StreamBox", "new_trace_id"]
