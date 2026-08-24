"""Every stored contract, one row each, and the two buttons that set the scope.

Built from `st.columns` inside a bordered container rather than from
`st.dataframe`: a row carries three buttons and a chip, and a dataframe carries
neither. That trade is worth revisiting at about twenty documents, when
pagination starts to matter more than the buttons do.

Every value on a row comes from the one `GET /documents` call -- pages,
passages, when it was added, and how its last analysis went. That is what the
endpoint was widened for: the alternative is a request per row on every
re-render, and a Streamlit script re-renders on every click.
"""

from __future__ import annotations

import streamlit as st

from .. import errors, state, theme
from ..client import ApiClient, ApiError, new_trace_id
from ..layout import escape, header

ISOLATION_NOTE = (
    "Each upload becomes its own document id. Retrieval, analysis and chat are scoped "
    "to one id, so a question about one contract can never quote another."
)

#: `st.columns` weights: Document · Id · Pages · Passages · Last analysis · Actions.
WIDTHS = [5, 1, 1, 1.4, 2.6, 3]


def render(api: ApiClient) -> None:
    try:
        documents = api.documents()
    except ApiError as exc:
        header("Document library", "")
        errors.show(exc, context="The library could not be loaded")
        return

    header(
        "Document library",
        f"{len(documents)} document{'' if len(documents) == 1 else 's'} · "
        "each one analysed and queried in isolation",
    )

    if not documents:
        _empty()
        return

    with st.container(border=True):
        _head()
        for document in documents:
            st.divider()
            _row(api, document)

    st.markdown(theme.note(ISOLATION_NOTE), unsafe_allow_html=True)

    if st.session_state.confirm_delete is not None:
        _confirm_delete(api)


def _empty() -> None:
    with st.container(border=True):
        st.markdown(
            f"<div style='text-align:center;padding:44px 12px'>"
            f"<div style='font-family:{theme.SERIF};font-size:19px;font-weight:600;"
            f"color:{theme.INK}'>No contracts yet</div>"
            f"<div style='font-family:{theme.SANS};font-size:14px;color:{theme.MUTED};"
            "margin-top:6px'>Upload a PDF and it becomes a document id you can analyse "
            "or ask questions about.</div></div>",
            unsafe_allow_html=True,
        )
    if st.button("Upload a contract", type="primary"):
        state.go("upload")
        st.rerun()


def _head() -> None:
    columns = st.columns(WIDTHS, vertical_alignment="center")
    for column, name in zip(
        columns, ("Document", "Id", "Pages", "Passages", "Last analysis", "Actions"), strict=True
    ):
        column.markdown(theme.label(name), unsafe_allow_html=True)


def _row(api: ApiClient, document: dict) -> None:
    document_id = document["document_id"]
    active = document_id == st.session_state.document_id
    columns = st.columns(WIDTHS, vertical_alignment="center")

    with columns[0]:
        st.markdown(
            f"<div style='font-family:{theme.SERIF};font-size:16px;font-weight:600;"
            f"color:{theme.INK}'>{escape(document['filename'])}"
            f"{' ·' if active else ''}</div>"
            f"<div style='font-family:{theme.SANS};font-size:12px;color:{theme.META}'>"
            f"added {_when(document.get('ingested_at', ''))}"
            f"{' · active' if active else ''}</div>",
            unsafe_allow_html=True,
        )
    columns[1].markdown(theme.meta(str(document_id)), unsafe_allow_html=True)
    columns[2].markdown(theme.meta(str(document.get("pages") or "—")), unsafe_allow_html=True)
    columns[3].markdown(theme.meta(str(document.get("chunks", 0))), unsafe_allow_html=True)
    columns[4].markdown(
        theme.last_analysis_chip(document.get("last_analysis")), unsafe_allow_html=True
    )

    with columns[5]:
        analyse, ask, remove = st.columns([2, 2, 1])
        # Both buttons set the scope *and* the view: that pairing is the reason
        # the tab row is a segmented control rather than `st.tabs`.
        if analyse.button("Analyse", key=f"an-{document_id}", use_container_width=True):
            state.go("analysis", document_id)
            st.rerun()
        if ask.button("Chat", key=f"ch-{document_id}", use_container_width=True):
            state.go("chat", document_id)
            st.rerun()
        if remove.button(
            ":material/delete:",
            key=f"rm-{document_id}",
            help="Delete this contract",
            use_container_width=True,
        ):
            st.session_state.confirm_delete = document
            st.rerun()


@st.dialog("Delete this contract?")
def _confirm_delete(api: ApiClient) -> None:
    """A confirmation, because a delete removes a contract, its passages and
    its vectors and cannot be undone.

    What it does *not* remove is the analyses: `analyses.document_id` carries
    no foreign key, because the report is the deliverable and it is
    self-contained. Saying so here is the difference between a reviewer
    hesitating and a reviewer knowing.
    """
    document = st.session_state.confirm_delete
    st.markdown(f"**{escape(document['filename'])}** (id {document['document_id']})")
    st.write(
        "Its passages, its vectors and the stored file go with it. Reports already "
        "produced for this contract are kept — a report is self-contained, and one "
        "that disappears because someone tidied up the corpus is not a record."
    )
    left, right = st.columns(2)
    if left.button("Cancel", use_container_width=True):
        st.session_state.confirm_delete = None
        st.rerun()
    if right.button("Delete", type="primary", use_container_width=True):
        trace_id = new_trace_id()
        st.session_state.trace_id = trace_id
        try:
            api.delete_document(document["document_id"], trace_id=trace_id)
        except ApiError as exc:
            # `409 analysis_running` lands here, which is the one refusal a
            # reviewer will actually meet: the run has to finish or be
            # cancelled first. The dialog is closed so the message is not
            # buried under it.
            errors.stash(exc)
        else:
            if st.session_state.document_id == document["document_id"]:
                st.session_state.document_id = None
                st.session_state.document = None
            state.set_analysis(document["document_id"], None)
            st.session_state.chat_history.pop(document["document_id"], None)
        st.session_state.confirm_delete = None
        st.rerun()


def _when(timestamp: str) -> str:
    """`2026-08-24T04:30:50Z` -> `24 Aug, 04:30`. Falls back to the raw value:
    a timestamp this cannot parse is still more useful than an empty cell."""
    from datetime import datetime

    if not timestamp:
        return "—"
    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return timestamp
    return moment.strftime("%d %b, %H:%M")
