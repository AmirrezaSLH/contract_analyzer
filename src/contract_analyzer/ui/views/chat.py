"""Ask this contract. Streamed, cited, and scoped to one document.

Top to bottom: the settings row, the transcript, three suggestion chips, the
input. Enter sends, natively, because the input is `st.chat_input`.

**The three settings are per question, not per conversation.** They apply to
the next question and do not re-run answers already on screen; the line beside
them says so. That matches the API, which has no session for them to belong to.

**Depth is an abstraction over `retrieval_top_k`, and the numbers never reach
the screen.** A compliance reviewer has no basis for choosing 4 passages over
8, but does have one for choosing "deep" when a clause is buried in an exhibit.
The mapping is `state.DEPTH_TOP_K`, and its middle value is set from the
server's configured default so that leaving the control alone and having no
control at all are the same thing.

**The transcript is the client's.** The API is stateless: this list is sent
back on every question, and `chat()` replays the last eight messages.
"""

from __future__ import annotations

import time

import streamlit as st

from .. import errors, state, theme
from ..client import ApiClient, ApiError, StreamBox, new_trace_id
from ..layout import escape, header

SUGGESTIONS = [
    "Does the contract require MFA for privileged access?",
    "How often must the asset inventory be reconciled?",
    "Is background screening required for vendor staff?",
]

RETRIEVAL_HELP = (
    "How the contract is searched. Hybrid fuses vector and keyword results — the safest "
    "for contract language, where a defined term matters as much as its meaning. Vector "
    "alone favours paraphrase; keyword alone favours exact wording."
)
DEPTH_HELP = (
    "How much of the contract is put in front of the model as evidence. Deep reaches "
    "clauses buried in exhibits but costs more and can pull in tangential text. Shallow "
    "is faster and tighter."
)


def render(api: ApiClient) -> None:
    document = st.session_state.document
    if document is None:
        return
    document_id = document["document_id"]
    health = st.session_state.health or {}

    header(
        "Ask this contract",
        "Every answer carries the clause and page it came from, checked against the "
        "source passage",
    )

    _settings_row(document, health)
    st.divider()

    for turn in state.history(document_id):
        avatar = theme.USER_AVATAR if turn["role"] == "user" else theme.ASSISTANT_AVATAR
        with st.chat_message(turn["role"], avatar=avatar):
            st.markdown(turn["content"])
            # Citations are re-rendered from what was stored with the turn:
            # re-asking the API to reproduce them would be a second dollar for
            # a transcript the client already holds.
            for citation in turn.get("citations") or []:
                st.html(theme.quote_card(citation))
            if turn.get("caption"):
                st.caption(turn["caption"])

    if not (st.session_state.health or {}).get("key_present", True):
        st.info(
            "**Chat needs an answer key, and none is configured.** Set "
            "`ANTHROPIC_API_KEY` in the API's `.env` and restart it. Upload and the "
            "library work without one.",
            icon=":material/info:",
        )
        return

    pending = _suggestions(document_id)
    typed = st.chat_input("Ask anything about this contract")
    question = typed or pending

    st.caption(
        f"Answers are drawn only from {document['filename']}. Nothing outside the "
        "active document is retrieved."
    )

    if question:
        _ask(api, document, question)


def _settings_row(document: dict, health: dict) -> None:
    """Model, Retrieval, Depth — and a line restating the selection in words.

    The model list comes from `/health`, not from a constant here: it is the
    allowlist `POST /chat` validates against, so a picker built from anything
    else can offer a choice the API will refuse.
    """
    models = health.get("chat_models") or [health.get("answer_model", "")]
    controls = st.columns([2.4, 1.7, 1.6, 4])

    with controls[0]:
        _control("Model", "chat_model", models)
    with controls[1]:
        _control("Retrieval", "chat_retrieval", ["hybrid", "vector", "keyword"],
                 help=RETRIEVAL_HELP)
    with controls[2]:
        _control("Depth", "chat_depth", list(state.DEPTH_TOP_K), help=DEPTH_HELP)
    with controls[3]:
        st.write("")
        st.markdown(
            f"<div style='text-align:right;font-family:{theme.SANS};font-size:13px;"
            f"color:{theme.META};padding-top:14px'>Applies to the next question · "
            f"{escape(st.session_state.chat_retrieval or 'hybrid')} retrieval at "
            f"{escape(st.session_state.chat_depth)} depth over "
            f"{escape(document['filename'])}</div>",
            unsafe_allow_html=True,
        )


def _control(label: str, key: str, options: list[str], *, help: str | None = None) -> None:
    """A selectbox backed by `session_state[key]`, clamped to its options.

    The clamp before the widget matters: Streamlit takes the value from
    `session_state` when the key exists, and raises if that value is not in
    the options. The value *can* fall outside them -- `answer_model` seeds
    `chat_model` from `/health`, and a deployment could publish an allowlist
    that no longer contains it -- so it is corrected here rather than left to
    become an exception on the second run.
    """
    if st.session_state.get(key) not in options:
        st.session_state[key] = options[0] if options else None
    st.selectbox(label, options, key=key, help=help)


def _suggestions(document_id: int) -> str | None:
    """Three real questions. A first-run affordance that stays visible: they
    are cheap and they teach a reviewer what this is for."""
    chosen = st.pills(
        "Suggestions", SUGGESTIONS, key=f"chips-{document_id}", label_visibility="collapsed"
    )
    if chosen:
        # Cleared immediately, or the pill stays selected and re-asks its
        # question on the next re-run.
        st.session_state[f"chips-{document_id}"] = None
        return chosen
    return None


def _ask(api: ApiClient, document: dict, question: str) -> None:
    document_id = document["document_id"]
    trace_id = new_trace_id()
    st.session_state.trace_id = trace_id
    history = [{"role": t["role"], "content": t["content"]} for t in state.history(document_id)]

    with st.chat_message("user", avatar=theme.USER_AVATAR):
        st.markdown(question)

    box = StreamBox()
    started = time.perf_counter()
    with st.chat_message("assistant", avatar=theme.ASSISTANT_AVATAR):
        try:
            stream = api.chat_stream(
                document_id,
                question,
                box,
                history=history,
                model=st.session_state.chat_model,
                retrieval_mode=st.session_state.chat_retrieval,
                top_k=state.top_k(),
                trace_id=trace_id,
            )
            # Returns the concatenation of everything yielded -- that is what
            # goes into the transcript, not the individual deltas.
            answer = st.write_stream(stream)
        except ApiError as exc:
            # Raised before the stream opened -- a 503 with no key, a document
            # that has just been deleted. No turn is appended: there is no
            # answer, and a transcript with a question and no reply is worse
            # than none.
            errors.show(exc, context="The question could not be answered")
            return

        elapsed = time.perf_counter() - started
        for citation in box.citations:
            st.html(theme.quote_card(citation))
        caption = theme.usage_line(box.usage, elapsed)
        st.caption(caption)
        if box.error is not None:
            # Mid-stream failure. The partial text above is kept and the turn
            # is marked incomplete -- never a bare spinner that never resolves.
            errors.show(box.error, context="The answer stopped partway")

    state.add_turn(document_id, "user", question)
    state.add_turn(
        document_id,
        "assistant",
        str(answer or ""),
        citations=box.citations,
        caption=caption + ("  ·  incomplete" if box.error else ""),
    )
    # The re-run redraws the transcript from `session_state`, which is what
    # makes this turn look like every earlier one rather than like a stream
    # that happens to have stopped.
    st.rerun()
