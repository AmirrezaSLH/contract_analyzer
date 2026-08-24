"""`ApiClient` itself: the plumbing the view tests stub out.

`test_ui.py` drives the app against a stub of this class, which is right for
what it asserts -- which view is drawn, what the scope is -- but it means the
class's own work is never executed there: header merging, the error envelope,
the SSE reader, the timeouts. A `TypeError` in `_request` was invisible to
twenty-seven passing view tests and reached the browser instead.

So this file exercises the client against an `httpx.MockTransport`: a real
`httpx2` request/response cycle with no server and no network, which is the
only way to test the argument plumbing at all.
"""

from __future__ import annotations

import json

import httpx2 as httpx
import pytest

from contract_analyzer.ui.client import ApiClient, ApiError, StreamBox


def client(handler, **over) -> ApiClient:
    return ApiClient(
        over.pop("base_url", "http://api.test"),
        api_key=over.pop("api_key", None),
        transport=httpx.MockTransport(handler),
    )


def responder(status=200, payload=None, *, record=None, headers=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        return httpx.Response(status, json=payload if payload is not None else {},
                              headers=headers or {})

    return handler


def sse(*frames: str, status=200, record=None):
    """An SSE body as the API writes it: `event:` then `data:`, blank between."""
    body = "".join(frames)

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        return httpx.Response(
            status, content=body.encode(), headers={"content-type": "text/event-stream"}
        )

    return handler


def frame(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# -- headers ----------------------------------------------------------------


def test_a_per_call_header_does_not_collide_with_the_standing_ones():
    """The regression. `_request` supplies `headers=` for the key and the trace
    id, so a caller that passed its own through `**kwargs` handed httpx the
    same argument twice -- a TypeError raised on the one path that needs it,
    submitting an analysis with an Idempotency-Key."""
    seen: list[httpx.Request] = []
    api = client(responder(202, {"analysis_id": "a1"}, record=seen), api_key="secret")

    api.create_analysis(7, trace_id="trace-1", idempotency_key="idem-9")

    headers = seen[0].headers
    assert headers["X-API-Key"] == "secret"
    assert headers["X-Trace-Id"] == "trace-1"
    assert headers["Idempotency-Key"] == "idem-9"


def test_no_idempotency_key_means_no_header_at_all():
    """Absent, not empty: the API's duplicate-submit guard branches on the
    header being there, and an empty one is a different question."""
    seen: list[httpx.Request] = []
    api = client(responder(202, {"analysis_id": "a1"}, record=seen))

    api.create_analysis(7, trace_id="trace-1")

    assert "Idempotency-Key" not in seen[0].headers


def test_the_key_is_omitted_when_there_is_none():
    seen: list[httpx.Request] = []
    api = client(responder(200, [], record=seen), api_key=None)

    api.documents()

    assert "X-API-Key" not in seen[0].headers


# -- the error envelope -----------------------------------------------------


def test_the_error_envelope_becomes_an_apierror_with_its_code_and_hint():
    """`code` is what a view branches on and `hint` is what a person reads, so
    both have to survive the trip."""
    api = client(responder(404, {"error": {
        "code": "document_not_found",
        "message": "No document with id 42.",
        "hint": "Pick a contract from the library, or upload one.",
    }}, headers={"X-Trace-Id": "t-9"}))

    with pytest.raises(ApiError) as caught:
        api.document(42)

    error = caught.value
    assert error.code == "document_not_found"
    assert error.message == "No document with id 42."
    assert error.hint.startswith("Pick a contract")
    assert error.status == 404
    # The trace id off the response, so an error surface can print the id that
    # is on every log line of the request that failed.
    assert error.trace_id == "t-9"


def test_a_failure_with_no_envelope_still_raises_something_actionable():
    """A proxy answering instead of the API. There is no `code` to read, so one
    is supplied rather than letting a KeyError escape."""
    def handler(request):
        return httpx.Response(502, content=b"<html>Bad Gateway</html>")

    api = client(handler)

    with pytest.raises(ApiError) as caught:
        api.documents()

    assert caught.value.status == 502
    assert caught.value.code == "http_error"


def test_a_connection_failure_is_unreachable_and_says_what_to_check():
    def handler(request):
        raise httpx.ConnectError("nothing is listening", request=request)

    api = client(handler)

    with pytest.raises(ApiError) as caught:
        api.health()

    assert caught.value.code == "unreachable"
    assert "CA_API_URL" in caught.value.hint


def test_a_204_is_not_parsed_as_json():
    """`DELETE /documents/{id}` answers 204 with no body; asking that for JSON
    is how a successful delete would come back as an error."""
    api = client(responder(204))

    assert api.delete_document(3) is None


# -- request shaping --------------------------------------------------------


def test_optional_chat_settings_are_omitted_rather_than_sent_as_null():
    """Each one is optional on the API and omitting it means "the configured
    default" -- which is exactly what a control nobody touched should mean."""
    seen: list[httpx.Request] = []
    api = client(responder(200, {"text": "", "citations": []}, record=seen))

    api.chat(1, "Why?", model=None, retrieval_mode=None, top_k=None)

    body = json.loads(seen[0].content)
    assert set(body) == {"document_id", "question", "history", "stream"}
    assert body["stream"] is False


def test_chat_settings_are_sent_when_they_are_set():
    seen: list[httpx.Request] = []
    api = client(responder(200, {"text": "", "citations": []}, record=seen))

    api.chat(1, "Why?", model="claude-haiku-4-5", retrieval_mode="keyword", top_k=12)

    body = json.loads(seen[0].content)
    assert body["model"] == "claude-haiku-4-5"
    assert body["retrieval_mode"] == "keyword"
    assert body["top_k"] == 12


def test_the_base_url_tolerates_a_trailing_slash():
    seen: list[httpx.Request] = []
    api = ApiClient(
        "http://api.test/", transport=httpx.MockTransport(responder(200, [], record=seen))
    )

    api.documents()

    assert str(seen[0].url) == "http://api.test/documents"


# -- the SSE reader ---------------------------------------------------------


def test_the_stream_yields_text_and_puts_everything_else_in_the_box():
    """Shaped for `st.write_stream`, which renders what is yielded and returns
    the concatenation -- so citations and usage, which are not text, have to
    land somewhere the caller still holds."""
    box = StreamBox()
    api = client(sse(
        frame("text", {"text": "TLS 1.2 "}),
        frame("tool_call", {"name": "search_contract", "returned": 6}),
        frame("text", {"text": "or higher."}),
        frame("citations", {"citations": [{"text": "quoted", "section_ref": "7.2",
                                           "verified": True}]}),
        frame("done", {"cost_usd": 0.03, "tool_calls": 1, "model": "claude-opus-5"}),
    ))

    chunks = list(api.chat_stream(1, "What TLS?", box))

    assert "".join(chunks) == "TLS 1.2 or higher."
    assert box.citations[0]["section_ref"] == "7.2"
    assert box.usage["model"] == "claude-opus-5"
    assert box.tool_calls[0]["name"] == "search_contract"
    assert box.error is None


def test_a_503_before_the_stream_opens_raises_rather_than_yielding():
    """The view depends on this: a failure before any text means no turn is
    appended at all, and a transcript holding a question with no reply is worse
    than one that never took the question."""
    def handler(request):
        return httpx.Response(503, json={"error": {
            "code": "no_api_key", "message": "ANTHROPIC_API_KEY is not set.",
            "hint": "Set it in .env and restart."}})

    api = client(handler)

    with pytest.raises(ApiError) as caught:
        list(api.chat_stream(1, "Why?", StreamBox()))

    assert caught.value.code == "no_api_key"


def test_an_error_event_mid_stream_keeps_the_text_that_arrived():
    """It cannot raise without discarding the partial answer, so it lands in
    the box and the view decides. Never a bare spinner."""
    box = StreamBox()
    api = client(sse(
        frame("text", {"text": "The vendor must "}),
        frame("error", {"code": "upstream_failure", "message": "The provider gave up."}),
    ))

    chunks = list(api.chat_stream(1, "Why?", box))

    assert "".join(chunks) == "The vendor must "
    assert box.error is not None
    assert box.error.code == "upstream_failure"


def test_keepalive_comments_and_malformed_frames_do_not_break_the_stream():
    """`ping` writes `: comment` lines, and a frame cut in half by a dropped
    connection is not a reason to lose the answer around it."""
    box = StreamBox()
    api = client(sse(
        ": keepalive\n\n",
        frame("text", {"text": "one "}),
        "event: text\ndata: {not json\n\n",
        frame("text", {"text": "two"}),
        frame("done", {"cost_usd": 0.01}),
    ))

    assert "".join(api.chat_stream(1, "Why?", box)) == "one two"
    assert box.usage == {"cost_usd": 0.01}
