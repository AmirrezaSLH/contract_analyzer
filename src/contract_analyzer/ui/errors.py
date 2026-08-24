"""Where an `ApiError` goes on the screen.

`01_ui_spec.md` §5 lists error surfaces as the highest-priority undesigned
work, and the reason it matters is in the API's own error table: every failure
already carries a stable `code` and a `hint` written for a person. What was
missing was somewhere to put them. This module is that somewhere, and it is one
module rather than four so that a code cannot be handled in the library view
and forgotten in chat.

The shape is the same everywhere: **what happened**, then **what to do about
it**, then the trace id when there is one. That order is deliberate -- a
reviewer who already knows what went wrong wants the second line, and a
reviewer who does not needs the first.

Three codes get more than a sentence, because they are the three a demo
actually hits:

* `no_api_key` / `answer_unavailable` -- the key is missing and the button
  should have been disabled. `/health` reports `key_present` for exactly this
  reason, so this is the second line of defence rather than the first.
* `embedder_unavailable` -- upload cannot index, and a document with no chunks
  answers every question with "no relevant passages". Better refused loudly.
* `unreachable` -- the API is not there at all, which under Docker means one
  command.
"""

from __future__ import annotations

import streamlit as st

from .client import ApiError

#: A headline per code. The API's `message` is accurate but often names an
#: internal noun ("ANTHROPIC_API_KEY is not set"); this is the sentence a
#: reviewer reads first. Anything not listed falls back to the message, which
#: is always safe -- it is written by this project, not echoed from upstream.
HEADLINES: dict[str, str] = {
    "no_api_key": "This needs an answer key, and none is configured.",
    "answer_unavailable": "This needs an answer key, and none is configured.",
    "embedder_unavailable": "This contract cannot be indexed right now.",
    "ingest_failed": "The contract could not be read.",
    "payload_too_large": "That file is over the upload limit.",
    "unsupported_media_type": "Only PDF contracts can be uploaded.",
    "document_not_found": "That contract is no longer here.",
    "analysis_not_found": "That analysis is no longer here.",
    "analysis_running": "An analysis of this contract is still running.",
    "not_running": "This analysis is not running in the worker you are talking to.",
    "not_live_here": "This analysis is not running in the worker you are talking to.",
    "model_mismatch": "This corpus was indexed with a different embedding model.",
    "upstream_failure": "The model provider did not answer.",
    "validation": "That request was not valid.",
    "unauthorized": "This API needs a key that this UI is not sending.",
    "metrics_unavailable": "Metrics are not available yet.",
    "unreachable": "The API is not reachable.",
    "bad_response": "The API returned something unexpected.",
    "internal": "The API failed to handle that.",
}

#: Codes that mean "the thing you are looking at is gone or busy", which is a
#: warning rather than an error: nothing broke, the state moved.
_WARNINGS = frozenset(
    {"document_not_found", "analysis_not_found", "analysis_running", "not_running",
     "not_live_here", "unsupported_media_type", "payload_too_large", "validation"}
)


def show(error: ApiError, *, context: str = "") -> None:
    """Render one failure. Call it where the thing that failed would have been.

    `context` is the action, in the user's words -- "The upload failed" -- and
    is prepended when the code has no headline of its own.
    """
    headline = HEADLINES.get(error.code)
    if headline is None:
        headline = f"{context}: {error.message}" if context else error.message
    body = [f"**{headline}**"]
    if headline != error.message:
        body.append(error.message)
    if error.hint:
        body.append(error.hint)
    if error.trace_id:
        body.append(f"`trace {error.trace_id}`")

    render = st.warning if error.code in _WARNINGS else st.error
    render("\n\n".join(body), icon=":material/error:")


def show_pending() -> None:
    """Render and clear an error stored for the next run.

    A failure inside a callback or just before an `st.rerun()` has nowhere to
    draw itself: the run it happened in is over. Stashing it in `session_state`
    and rendering it at the top of the next one is how it survives, and
    clearing it here is what stops it outliving the thing it is about.
    """
    error = st.session_state.get("error")
    if error is not None:
        st.session_state.error = None
        show(error)


def stash(error: ApiError) -> None:
    st.session_state.error = error


def guard(context: str = ""):
    """`with guard("The upload failed"):` -- render an `ApiError` in place.

    Only `ApiError` is caught. Anything else is a bug in this UI and should
    reach Streamlit's own handler with its traceback intact, where it can be
    read and fixed, rather than being flattened into a polite sentence.
    """
    return _Guard(context)


class _Guard:
    def __init__(self, context: str) -> None:
        self.context = context
        self.error: ApiError | None = None

    def __enter__(self) -> _Guard:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if isinstance(exc, ApiError):
            self.error = exc
            show(exc, context=self.context)
            return True
        return False


__all__ = ["HEADLINES", "guard", "show", "show_pending", "stash"]
