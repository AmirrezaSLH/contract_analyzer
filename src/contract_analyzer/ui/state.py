"""Every `session_state` key, declared once, with its default.

Streamlit re-runs the whole script on every interaction, so `session_state` is
the only thing that survives between runs -- which makes an undeclared key a
`KeyError` on the second click rather than the first. Declaring them in one
dict means `init()` is the whole contract, and a view reading a key it did not
set is a name that is visible here rather than a bug that is not.

Two keys are dicts keyed by document id rather than plain values:
`analysis_id` and `chat_history`. That is the same scoping rule the rest of the
product runs on -- switching contracts must not carry one contract's transcript
onto another, and it must not lose the analysis you left running.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

#: Cap on the stored transcript. `chat()` replays the last 8 messages, so this
#: is not about the model -- it is about a session that stays open all day not
#: growing without bound in this process's memory.
MAX_TURNS = 50

VIEWS = ("upload", "library", "analysis", "chat")

#: `Depth` -> `retrieval_top_k`. The one place this UI knowingly hides a
#: parameter: a compliance reviewer has no basis for choosing 4 passages over
#: 8, but does have one for choosing "deep" when a clause is buried in an
#: exhibit. `medium` is set from `/health` at boot so the default choice and
#: the configured default are the same thing; the other two are relative to it
#: and should be re-set from a real recall measurement when there is one.
DEPTH_TOP_K = {"shallow": 3, "medium": 6, "deep": 12}

DEFAULTS: dict[str, Any] = {
    "view": "upload",
    #: The scope. Everything else reads this, and nothing on screen may come
    #: from another document.
    "document_id": None,
    #: The last `GET /documents/{id}` payload, for the sidebar's meta line.
    "document": None,
    #: `GET /health` at boot: the model list, the retrieval defaults, the
    #: upload cap, and whether there is an answer key.
    "health": None,
    "health_error": None,
    #: `{criterion_id: title}` from `GET /criteria`, fetched once. The
    #: progress table has only ids to work with -- `CriterionProgress` carries
    #: no title -- and `data_in_transit` is not a name to put in front of a
    #: reviewer.
    "criteria_titles": {},
    #: `{document_id: analysis_id}` -- the run this session is watching.
    "analysis_id": {},
    #: `{document_id: [{role, content}]}`. The API is stateless; this is the
    #: transcript, and it is sent back on every question.
    "chat_history": {},
    #: Which criterion row is expanded. One at a time.
    "open_criterion": None,
    #: The three chat controls. Per session, not per document: a model choice
    #: is an operator preference, not a property of a contract.
    "chat_model": None,
    "chat_retrieval": None,
    "chat_depth": "medium",
    #: Minted per user-initiated action and displayed on the analysis card.
    #: This is what makes the log walkthrough possible.
    "trace_id": None,
    #: The `201 Document` body of the most recent upload, so the result card
    #: persists. It is not a toast: it carries the document id.
    "upload_result": None,
    #: An `ApiError` a view wants shown at the top of the next run.
    "error": None,
    #: The document a delete confirmation is open for.
    "confirm_delete": None,
}


def init() -> None:
    """Fill in anything missing. Safe to call on every run, which it is."""
    for key, value in DEFAULTS.items():
        if key not in st.session_state:
            # A fresh copy per session: a shared mutable default would let one
            # session's transcript appear in another's.
            st.session_state[key] = dict(value) if isinstance(value, dict) else value


def go(view: str, document_id: int | None = None) -> None:
    """Switch view, optionally re-scoping first.

    The order matters: the scope is set before the view, so a view rendered on
    the next run never sees the previous document. This is the function the
    library's **Analyse** and **Chat** buttons call, and it is why the tab row
    is `st.segmented_control` rather than `st.tabs` -- a tab set cannot be
    switched from a button.
    """
    if document_id is not None and document_id != st.session_state.document_id:
        st.session_state.document_id = document_id
        st.session_state.document = None
        st.session_state.open_criterion = None
    st.session_state.view = view


def history(document_id: int) -> list[dict[str, str]]:
    return st.session_state.chat_history.setdefault(document_id, [])


def add_turn(document_id: int, role: str, content: str, **extra) -> None:
    """Append one turn, keeping the transcript bounded -- see `MAX_TURNS`.

    `extra` carries what an assistant turn needs to be *re-rendered* without
    asking the API to produce it again: its citations and its usage caption.
    Only `role` and `content` are sent back as history; the rest is this
    client's copy of what it already paid for.
    """
    turns = history(document_id)
    turns.append({"role": role, "content": content, **extra})
    if len(turns) > MAX_TURNS:
        del turns[: len(turns) - MAX_TURNS]


def analysis_id(document_id: int) -> str | None:
    return st.session_state.analysis_id.get(document_id)


def set_analysis(document_id: int, value: str | None) -> None:
    if value is None:
        st.session_state.analysis_id.pop(document_id, None)
    else:
        st.session_state.analysis_id[document_id] = value


def top_k() -> int:
    """The passage count the current Depth means. Never shown to the user."""
    return DEPTH_TOP_K.get(st.session_state.chat_depth, DEPTH_TOP_K["medium"])


def apply_health(health: dict) -> None:
    """Seed the controls from the server's configuration, once.

    The defaults the UI shows have to be the defaults the API would apply, or
    the settings row is lying about what happens if you leave it alone. Only
    unset keys are filled: a user who has already chosen a model keeps it
    across a re-render.
    """
    st.session_state.health = health
    if st.session_state.chat_model is None:
        st.session_state.chat_model = health.get("answer_model")
    if st.session_state.chat_retrieval is None:
        st.session_state.chat_retrieval = health.get("retrieval_mode", "hybrid")
    configured = health.get("retrieval_top_k")
    if configured:
        # `medium` *is* the configured default, whatever that is. The mapping
        # is the frontend's, but its middle value is not the frontend's to
        # disagree with the backend about.
        DEPTH_TOP_K["medium"] = int(configured)


__all__ = [
    "DEFAULTS",
    "DEPTH_TOP_K",
    "MAX_TURNS",
    "VIEWS",
    "add_turn",
    "analysis_id",
    "apply_health",
    "go",
    "history",
    "init",
    "set_analysis",
    "top_k",
]
