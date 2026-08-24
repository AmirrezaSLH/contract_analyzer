"""Upload a contract, and say what happened to it.

The drop zone is the whole page until something is uploaded. On success a
result card appears and **stays** -- it is not a toast, because the thing it
carries is the document id, which is what the user needs next.

The explanatory strip underneath is the only explanatory copy in the product.
It is here because upload is where a first-time reviewer is most lost, and it
is four sentences. Do not let it grow.
"""

from __future__ import annotations

import streamlit as st

from .. import errors, state, theme
from ..client import ApiClient, new_trace_id
from ..layout import escape, header

WHAT_HAPPENS = [
    ("1 · Parse", "Text, tables and exhibits are extracted page by page, with the heading "
                  "outline kept as the spine."),
    ("2 · Chunk", "400-token passages with 80-token overlap, each carrying its section path "
                  "and page."),
    ("3 · Index", "Embeddings and full-text rows, so retrieval is hybrid and scoped to this "
                  "contract alone."),
    ("4 · Ready", "A document id you can analyse against the five criteria, or open a chat "
                  "over."),
]


def render(api: ApiClient) -> None:
    limit = int((st.session_state.health or {}).get("max_upload_mb", 25))
    header(
        "Upload a contract",
        f"PDF up to {limit} MB · parsed, chunked and indexed before it comes back with "
        "a document id",
    )

    uploaded = st.file_uploader(
        "Drag and drop a contract here",
        type=["pdf"],
        # The literal cap from `/health`, so this line and the API's 413 agree.
        # Streamlit's own `maxUploadSize` is set to the same number in
        # config.toml; this is the sentence a person reads.
        help=f"Limit {limit} MB per file · PDF only",
        accept_multiple_files=False,
    )

    if uploaded is not None and _is_new(uploaded):
        _ingest(api, uploaded)

    result = st.session_state.upload_result
    if result is not None:
        _result_card(result)

    st.divider()
    st.markdown(theme.label("What happens on upload"), unsafe_allow_html=True)
    for column, (title, body) in zip(st.columns(4), WHAT_HAPPENS, strict=True):
        with column, st.container(border=True):
            st.markdown(
                f"<div style='font-family:{theme.SERIF};font-size:15px;font-weight:600;"
                f"color:{theme.INK};margin-bottom:6px'>{title}</div>"
                f"<div style='font-family:{theme.SANS};font-size:13px;line-height:1.55;"
                f"color:{theme.MUTED}'>{body}</div>",
                unsafe_allow_html=True,
            )


def _is_new(uploaded) -> bool:
    """Whether this is a file we have not already ingested.

    Streamlit hands back the same `UploadedFile` on every re-run until it is
    cleared, so without this the widget would re-upload its file on every
    click -- and every upload mints a new document id, so that is not a
    harmless repeat. `file_id` is stable for one selection and changes when the
    user picks a different file.
    """
    return st.session_state.get("uploaded_file_id") != uploaded.file_id


def _ingest(api: ApiClient, uploaded) -> None:
    st.session_state.uploaded_file_id = uploaded.file_id
    # One user action, one trace: this id is on every log line of the parse,
    # the chunking and the embedding.
    trace_id = new_trace_id()
    st.session_state.trace_id = trace_id
    with st.spinner(f"Parsing, chunking and indexing {uploaded.name}…"), errors.guard(
        "The upload failed"
    ) as guard:
        st.session_state.upload_result = api.upload(
            uploaded.name, uploaded.getvalue(), trace_id=trace_id
        )
    if guard.error is not None:
        # A rejected upload leaves nothing behind on the API side -- the
        # partial file is deleted as the cap trips -- so there is nothing to
        # clean up here either. The card is cleared so a failure does not sit
        # under a stale success.
        st.session_state.upload_result = None
        return
    document_id = st.session_state.upload_result["document_id"]
    state.go("upload", document_id)
    st.rerun()


def _result_card(result: dict) -> None:
    with st.container(border=True):
        st.markdown(
            f"<div style='display:flex;align-items:center;gap:9px;margin-bottom:14px'>"
            f"<span style='width:9px;height:9px;border-radius:50%;background:#2F6B4F'></span>"
            f"<span style='font-family:{theme.SERIF};font-size:19px;font-weight:600;"
            f"color:{theme.INK}'>{escape(result['filename'])} is ready</span>"
            f"<span style='font-family:{theme.SANS};font-size:13px;color:{theme.META}'>"
            f"ingested in {result.get('elapsed_s', 0):.1f} s</span></div>",
            unsafe_allow_html=True,
        )
        tiles = st.columns(4)
        tiles[0].metric("Document id", result["document_id"])
        tiles[1].metric("Pages", result.get("pages") or "—")
        tiles[2].metric("Passages", result.get("chunks", 0))
        # `spine_source`: whether the breadcrumbs were read from the PDF or
        # inferred. A reviewer checking a citation that names a section
        # deserves to know which.
        tiles[3].metric("Outline from", result.get("spine_source", "none"))

        st.write("")
        left, right, _ = st.columns([2, 2, 4])
        if left.button("Run compliance analysis", type="primary", use_container_width=True):
            state.go("analysis", result["document_id"])
            st.rerun()
        if right.button("Ask a question instead", use_container_width=True):
            state.go("chat", result["document_id"])
            st.rerun()
