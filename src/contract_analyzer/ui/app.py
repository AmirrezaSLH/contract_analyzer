"""One page, one view switch, one bespoke sidebar.

`st.navigation` / `st.Page` would own the sidebar, and the sidebar here is not
a page list -- it is an upload button, a library link, the document list and
the scope indicator. So this is a single script that branches on
`session_state["view"]`, which is less machinery and matches the design.

**The shape: scope in the sidebar, views in the tabs.** Everything is scoped to
one document, because that is a library invariant rather than a UI convention:
`retrieve()`, `chat()` and `analyze_document()` all take a `document_id`, and
the API never passes `ALL_DOCUMENTS`. Picking a document in the sidebar is what
sets the scope; **Analysis** and **Chat** are views *of* that document and are
rendered only when there is one. Upload and Library are application-level and
have no tab row, because they are not views of a document.

The tab row is `st.segmented_control`, not `st.tabs`. Two reasons, both
structural: a tab set cannot be switched programmatically, and the Library's
**Analyse** and **Chat** buttons must land the user on a specific view of a
specific document; and `st.tabs` executes every tab body on every run, which
would render the chat transcript on every two-second analysis poll.
"""

from __future__ import annotations

import contextlib

import streamlit as st

# Absolute, unlike every other module here. `streamlit run` executes this file
# as `__main__` with no package context, so a relative import raises
# `ImportError: attempted relative import with no known parent package` the
# moment a browser connects. The package is installed (editable), so the
# absolute form resolves; the view modules keep relative imports, because they
# *are* imported as package modules.
from contract_analyzer.ui import errors, state, theme
from contract_analyzer.ui.client import ApiClient, ApiError
from contract_analyzer.ui.layout import escape
from contract_analyzer.ui.views import analysis, chat, library, upload

TABS = {"analysis": "Analysis", "chat": "Chat"}


def get_client() -> ApiClient:
    """One client per session. Cheap to build; cached so the base URL and the
    key are resolved once rather than on every run."""
    if "api_client" not in st.session_state:
        st.session_state.api_client = ApiClient()
    return st.session_state.api_client


def boot(api: ApiClient) -> None:
    """`GET /health` once per session.

    It answers three questions the UI would otherwise guess at: whether there
    is an answer key (so a button can be disabled rather than discovered to be
    useless three clicks later), which models chat may offer, and what the
    retrieval defaults and the upload cap are. Hardcoding any of those here
    would create a copy that drifts the moment `settings.json` changes.
    """
    if st.session_state.health is not None or st.session_state.health_error is not None:
        return
    try:
        state.apply_health(api.health())
    except ApiError as exc:
        st.session_state.health_error = exc
        return
    # The compliance vocabulary, once. `/criteria` is open even when a key is
    # required, and it is the only place the real titles live: a progress row
    # carries an id and nothing else. Suppressed rather than reported: without
    # it the progress table falls back to prettifying the id, which is worse
    # copy but not a broken screen.
    with contextlib.suppress(ApiError):
        st.session_state.criteria_titles = {
            criterion["id"]: criterion["requirement"] for criterion in api.criteria()
        }


def sidebar(api: ApiClient) -> None:
    st.sidebar.markdown(
        f"<div style='font-family:{theme.SERIF};font-size:21px;font-weight:600;"
        f"color:{theme.INK};margin-bottom:2px'>Contract Analyzer</div>"
        f"<div style='font-family:{theme.SANS};font-size:13px;color:{theme.META};"
        "margin-bottom:22px'>Compliance review workspace</div>",
        unsafe_allow_html=True,
    )

    if st.sidebar.button("Upload a contract", type="primary", use_container_width=True):
        state.go("upload")
        st.rerun()

    documents: list[dict] = []
    try:
        documents = api.documents()
    except ApiError as exc:
        with st.sidebar:
            errors.show(exc, context="The document list could not be loaded")

    if st.sidebar.button(
        f"Library · {len(documents)}", type="tertiary", use_container_width=True
    ):
        state.go("library")
        st.rerun()

    if documents:
        st.sidebar.markdown(theme.label("Documents"), unsafe_allow_html=True)
    for document in documents:
        document_id = document["document_id"]
        active = document_id == st.session_state.document_id
        pages = document.get("pages") or "?"
        # Never "chunk" in customer-facing copy: a passage is what a reviewer
        # is being shown when a citation resolves.
        summary = f"{pages} pages · {document.get('chunks', 0)} passages"
        if st.sidebar.button(
            f"{'**' if active else ''}{document['filename']}{'**' if active else ''}\n\n{summary}",
            key=f"nav-{document_id}",
            type="tertiary",
            use_container_width=True,
        ):
            state.go("analysis", document_id)
            st.rerun()

    active = _active_document(documents)
    if active is not None:
        st.session_state.document = active
        st.sidebar.divider()
        st.sidebar.markdown(theme.label("Active document"), unsafe_allow_html=True)
        st.sidebar.markdown(
            f"<div style='font-family:{theme.SERIF};font-size:16px;font-weight:600;"
            f"color:{theme.INK}'>{escape(active['filename'])}</div>"
            f"<div style='font-family:{theme.SANS};font-size:12px;color:{theme.META}'>"
            f"id {active['document_id']} · {active.get('pages') or '?'} pages · "
            f"{active.get('chunks', 0)} passages</div>",
            unsafe_allow_html=True,
        )

    if st.session_state.trace_id:
        st.sidebar.divider()
        st.sidebar.markdown(theme.label("Trace"), unsafe_allow_html=True)
        st.sidebar.code(st.session_state.trace_id, language=None)

    _health_footer()


def _active_document(documents: list[dict]) -> dict | None:
    """The selected document, from the list already fetched.

    Read out of the list rather than with a second `GET /documents/{id}`: the
    sidebar needs the name and two counts, the list carries all three, and a
    request per run for data already in hand is the N+1 this endpoint was
    widened to avoid.

    A document that has vanished -- deleted in another tab -- clears the scope
    rather than leaving the views to 404 one at a time.
    """
    document_id = st.session_state.document_id
    if document_id is None:
        return None
    for document in documents:
        if document["document_id"] == document_id:
            return document
    st.session_state.document_id = None
    st.session_state.document = None
    return None


def _health_footer() -> None:
    if st.session_state.health_error is not None:
        with st.sidebar:
            errors.show(st.session_state.health_error)
        return
    health = st.session_state.health or {}
    if not health:
        return
    ok = health.get("status") == "ok"
    st.sidebar.caption(
        f"{'🟢' if ok else '🟠'} API {health.get('version', '')} · "
        f"{health.get('embedder', '')} embeddings"
        + ("" if health.get("key_present") else " · no answer key")
    )


def main() -> None:
    st.set_page_config(
        page_title="Contract Analyzer",
        page_icon=theme.ASSISTANT_AVATAR,
        layout="wide",
        initial_sidebar_state="expanded",
    )
    state.init()
    theme.inject()
    api = get_client()
    boot(api)
    sidebar(api)

    view = st.session_state.view
    # A view of a document with no document is not a state the user can be
    # left in: it would render an empty page with a tab row above it.
    if view in TABS and st.session_state.document_id is None:
        view = st.session_state.view = "library"

    errors.show_pending()

    if view in TABS:
        chosen = st.segmented_control(
            "View",
            list(TABS),
            format_func=TABS.get,
            default=view,
            key="tab_row",
            label_visibility="collapsed",
        )
        if chosen and chosen != view:
            state.go(chosen)
            st.rerun()

    if view == "upload":
        upload.render(api)
    elif view == "library":
        library.render(api)
    elif view == "analysis":
        analysis.render(api)
    elif view == "chat":
        chat.render(api)


main()
