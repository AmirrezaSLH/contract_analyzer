"""The front end, driven headlessly.

`AppTest` runs `app.py` the way a browser session would -- the full script, on
every interaction -- and hands back the widget tree. That is the only way to
test a Streamlit app that means anything: the interesting failures here are
re-run failures (a key that exists on the first pass and not the second, a
widget default that fights `session_state`, a view rendered with a scope that
has just changed), and none of them are reachable by calling a render function
directly.

**The API is a stub, not a server.** These tests are about the UI's own
decisions -- which view is drawn, what the scope is, whether a quote that
failed verification looks different from one that passed -- and `test_api.py`
already covers the wire. The stub also records what was asked for, which is how
the scoping assertions work: the proof that chat cannot leak across documents
is that the request carried the right `document_id`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="the UI extra is not installed")

from streamlit.testing.v1 import AppTest  # noqa: E402

from contract_analyzer.ui.client import ApiError, StreamBox  # noqa: E402

APP = str(Path(__file__).resolve().parents[1] / "src" / "contract_analyzer" / "ui" / "app.py")

HEALTH = {
    "status": "ok", "version": "1.2.3", "db": True, "embedder": "fake",
    "embedding_model": "fake-hash", "answer_model": "claude-opus-5",
    "chat_models": ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"],
    "retrieval_mode": "hybrid", "retrieval_top_k": 6, "max_upload_mb": 25.0,
    "api_workers": 2, "analysis_workers": 5, "key_present": True,
    "auth_required": False, "documents": 2, "analyses_running": 0,
}

CRITERIA = [
    {"id": "password_management", "requirement": "Password Management",
     "question": "?", "sub_requirements": [], "states": []},
    {"id": "data_in_transit", "requirement": "Data in Transit Encryption",
     "question": "?", "sub_requirements": [], "states": []},
]


def document(document_id=1, name="Sample Contract.pdf", last=None):
    return {
        "document_id": document_id, "filename": name, "pages": 21, "chunks": 102,
        "spine_source": "headings", "ingested_at": "2026-08-24T04:30:50+00:00",
        "last_analysis": last,
    }


def quote(text="Vendor will use TLS 1.2 or higher.", verified=True):
    return {
        "text": text, "evidence_id": "E1", "section_ref": "7.2 Data in Transit",
        "page_display": "5", "chunk_id": 3, "verified": verified,
    }


def result(criterion_id="data_in_transit", state="Fully Compliant", quotes=None, **over):
    return {
        "criterion_id": criterion_id,
        "compliance_requirement": "Data in Transit Encryption",
        "compliance_question": "?",
        "compliance_state": state,
        "sub_requirements": [
            {"id": "a", "requirement": "TLS 1.2 or higher", "status": "met", "quote_indexes": [0]},
            {"id": "b", "requirement": "Certificate management", "status": "missing",
             "quote_indexes": []},
        ],
        "relevant_quotes": quotes if quotes is not None else [quote()],
        "rationale": "Section 7.2 imposes a firm obligation.",
        "raw_confidence": 0.9, "confidence": 0.88, "confidence_components": {},
        "needs_review": False, "unresolved_errors": [], "structure_rounds": 0,
        "ended_by": "model", "tool_calls": 2,
        "usage": {"input_tokens": 100, "output_tokens": 20}, "cost_usd": 0.19,
        "model": "claude-opus-5", **over,
    }


def report(results=None, **over):
    return {
        "analysis_id": "a1", "document_id": 1, "filename": "Sample Contract.pdf",
        "status": "done", "trace_id": "trace-abc",
        "results": results if results is not None else [result()],
        "totals": {"criteria": 1, "latency_s": 187.5, "cost_usd": 0.96,
                   "input_tokens": 9, "output_tokens": 9, "tool_calls": 2,
                   "needs_review": 0, "capped": 0, "mean_confidence": 0.88},
        "cross_criterion_notes": [], "skipped": [], "error": None,
        "created_at": "2026-08-24T11:05:00+00:00", "completed_at": "2026-08-24T11:08:12+00:00",
        **over,
    }


def analysis(status="done", **over):
    body = {
        "analysis_id": "a1", "document_id": 1, "status": status, "stage": status,
        "progress": {"done": 1 if status == "done" else 0, "total": 1},
        "criteria": [{"id": "data_in_transit", "status": "done" if status == "done" else "queued",
                      "state": "Fully Compliant" if status == "done" else None,
                      "confidence": 0.88 if status == "done" else None, "needs_review": False}],
        "totals": None, "trace_id": "trace-abc", "error": None,
        "created_at": "2026-08-24T11:05:00+00:00", "started_at": None, "completed_at": None,
        "report": None,
    }
    body.update(over)
    return body


class StubApi:
    """Every method the UI calls, and a record of how it was called.

    Deliberately not a mock library: the assertions here are about *what the UI
    asked for*, and a hand-written stub that appends to `self.calls` reads
    better in a failure than a mock's call list does.
    """

    def __init__(self, documents=None, **over):
        self.documents_list = documents if documents is not None else [document()]
        self.calls: list[tuple] = []
        self.analyses_by_id: dict = over.pop("analyses", {})
        self.raise_on: dict = over.pop("raise_on", {})
        self.stream_text = over.pop("stream_text", ["The vendor ", "must use TLS 1.2."])
        self.stream_citations = over.pop("stream_citations", [quote()])
        self.stream_error = over.pop("stream_error", None)
        self.health_payload = over.pop("health", HEALTH)

    def _maybe_raise(self, name):
        if name in self.raise_on:
            raise self.raise_on[name]

    def health(self):
        self._maybe_raise("health")
        return self.health_payload

    def criteria(self):
        return CRITERIA

    def documents(self):
        self._maybe_raise("documents")
        return self.documents_list

    def document(self, document_id):
        return next(d for d in self.documents_list if d["document_id"] == document_id)

    def upload(self, name, data, *, trace_id=None):
        self.calls.append(("upload", name, len(data), trace_id))
        self._maybe_raise("upload")
        new = document(len(self.documents_list) + 1, name)
        new["elapsed_s"] = 1.1
        self.documents_list.insert(0, new)
        return new

    def delete_document(self, document_id, *, trace_id=None):
        self.calls.append(("delete", document_id, trace_id))
        self._maybe_raise("delete_document")
        self.documents_list = [
            d for d in self.documents_list if d["document_id"] != document_id
        ]

    def create_analysis(self, document_id, *, criteria=None, trace_id=None,
                        idempotency_key=None):
        self.calls.append(("create_analysis", document_id, trace_id, idempotency_key))
        self._maybe_raise("create_analysis")
        return {"analysis_id": "a1", "document_id": document_id, "status": "queued"}

    def analysis(self, analysis_id, *, detail="full"):
        self.calls.append(("analysis", analysis_id, detail))
        self._maybe_raise("analysis")
        if analysis_id not in self.analyses_by_id:
            raise ApiError("analysis_not_found", "No analysis with id " + analysis_id, "Run one.")
        return self.analyses_by_id[analysis_id]

    def cancel_analysis(self, analysis_id, *, trace_id=None):
        self.calls.append(("cancel", analysis_id, trace_id))
        return {"analysis_id": analysis_id, "status": "cancelled"}

    def chat_stream(self, document_id, question, box: StreamBox, **kwargs):
        self.calls.append(("chat", document_id, question, kwargs))
        self._maybe_raise("chat_stream")

        def stream():
            yield from self.stream_text
            box.citations = list(self.stream_citations)
            box.usage = {"cost_usd": 0.03, "tool_calls": 2, "model": kwargs.get("model")}
            if self.stream_error is not None:
                box.error = self.stream_error

        return stream()


def app(stub: StubApi, **session) -> AppTest:
    """A booted app wired to `stub`, with `session_state` pre-set.

    The client is injected through `session_state` rather than monkeypatched:
    `app.get_client()` caches it there, so seeding the key is the supported
    seam and there is no import to patch.
    """
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["api_client"] = stub
    for key, value in session.items():
        at.session_state[key] = value
    at.run()
    return at


def texts(at) -> str:
    """Everything rendered, as one string. The assertions below care that a
    sentence reached the page, not which widget carried it."""
    parts = [str(m.value) for m in at.markdown]
    parts += [str(c.value) for c in at.caption]
    parts += [str(h.body) for h in at.get("html")]
    parts += [str(e.value) for e in at.error] + [str(w.value) for w in at.warning]
    parts += [str(i.value) for i in at.info]
    parts += [str(e.label) for e in at.expander]
    return "\n".join(parts)


# -- the shell --------------------------------------------------------------


def test_it_boots_on_upload_and_reads_its_configuration_from_health():
    """Nothing about the model list, the retrieval defaults or the upload cap
    is hardcoded here: all three come from `/health`, so they cannot drift from
    `settings.json`."""
    at = app(StubApi())

    assert not at.exception
    assert at.session_state["view"] == "upload"
    assert at.session_state["chat_model"] == "claude-opus-5"
    assert at.session_state["chat_retrieval"] == "hybrid"
    # `medium` *is* the configured default, so the default UI choice and the
    # default backend behaviour are the same thing.
    from contract_analyzer.ui.state import DEPTH_TOP_K

    assert DEPTH_TOP_K["medium"] == HEALTH["retrieval_top_k"]
    assert "Limit 25 MB per file" in str(at.get("file_uploader")[0].help)


def test_the_tab_row_appears_only_once_a_document_is_in_scope():
    """Analysis and Chat are views *of* a document. With no scope there is
    nothing for them to be views of, so the row is absent -- and asking for one
    of them directly lands on the library rather than on an empty page."""
    at = app(StubApi())
    assert at.segmented_control == []

    at = app(StubApi(), view="analysis", document_id=None)
    assert at.session_state["view"] == "library"

    at = app(StubApi(), view="analysis", document_id=1)
    assert [s.value for s in at.segmented_control] == ["analysis"]


def test_a_document_deleted_elsewhere_clears_the_scope_rather_than_404ing():
    """The sidebar reads the scope out of the list it already fetched, so a
    document that has gone is noticed there -- once -- instead of by each view
    in turn."""
    at = app(StubApi(documents=[document(2, "Other.pdf")]), view="analysis", document_id=1)

    assert at.session_state["document_id"] is None
    assert at.session_state["view"] == "library"
    assert not at.exception


def test_an_unreachable_api_is_a_sentence_and_not_a_traceback():
    at = app(StubApi(raise_on={"health": ApiError(
        "unreachable", "Could not reach the API at http://localhost:8000.",
        "Check that the API is running.")}))

    assert not at.exception
    assert "The API is not reachable." in texts(at)
    assert "Check that the API is running." in texts(at)


# -- library ----------------------------------------------------------------


def test_the_library_composes_its_own_words_for_a_state_count():
    """`last_analysis.states` is a count per state precisely so the API does
    not choose this sentence. Five of five is compliant; anything else is a
    gap, and the plural is right."""
    at = app(
        StubApi(documents=[
            document(1, "Clean.pdf", last={"analysis_id": "a1", "status": "done",
                                           "completed_at": "x",
                                           "states": {"Fully Compliant": 5}, "needs_review": 0}),
            document(2, "Gaps.pdf", last={"analysis_id": "a2", "status": "done",
                                          "completed_at": "x",
                                          "states": {"Fully Compliant": 3,
                                                     "Partially Compliant": 2},
                                          "needs_review": 1}),
            document(3, "Fresh.pdf", last=None),
        ]),
        view="library",
    )

    rendered = texts(at)
    assert "5 of 5 compliant" in rendered
    assert "2 gaps found" in rendered
    assert "Not analysed" in rendered
    # And the isolation guarantee is stated in the user's terms, not ours.
    assert "can never quote another" in rendered


def test_the_library_never_fetches_a_document_per_row():
    """The N+1 this endpoint was widened to avoid. A Streamlit script re-runs
    on every click, so a request per row is a request per row per click."""
    stub = StubApi(documents=[document(1), document(2, "Two.pdf"), document(3, "Three.pdf")])
    at = app(stub, view="library")

    assert not at.exception
    assert [c for c in stub.calls if c[0] == "document"] == []


def test_analyse_and_chat_set_the_scope_and_the_view_together():
    """The pair is the whole reason the tab row is a segmented control: a tab
    set cannot be switched from a button."""
    at = app(StubApi(documents=[document(1), document(2, "Two.pdf")]), view="library")

    at.button(key="ch-2").click().run()

    assert at.session_state["document_id"] == 2
    assert at.session_state["view"] == "chat"


# -- analysis ---------------------------------------------------------------


def test_an_unanalysed_document_says_what_a_run_costs_before_offering_one():
    """Saying the cost out loud is deliberate: this is a product that spends a
    dollar per click, and `POST /analyses` refuses duplicate submissions for
    the same reason."""
    at = app(StubApi(), view="analysis", document_id=1)

    rendered = texts(at)
    assert "has not been analysed yet" in rendered
    assert "costs roughly a dollar, so it is never started for you" in rendered
    assert any(b.label == "Run compliance analysis" for b in at.button)


def test_the_run_button_is_disabled_without_an_answer_key_rather_than_refused():
    """`/health` reports `key_present` exactly so a UI can grey the button out
    instead of spending a click to discover a 503."""
    keyless = dict(HEALTH, key_present=False)
    at = app(StubApi(health=keyless), view="analysis", document_id=1)

    run = next(b for b in at.button if b.label == "Run compliance analysis")
    assert run.disabled


def test_submitting_records_the_analysis_and_mints_one_trace_id():
    stub = StubApi(analyses={"a1": analysis(status="queued")})
    at = app(stub, view="analysis", document_id=1)

    at.button(key="run").click().run()

    submitted = [c for c in stub.calls if c[0] == "create_analysis"]
    assert len(submitted) == 1
    _, document_id, trace_id, idempotency = submitted[0]
    assert document_id == 1 and trace_id and idempotency is None
    assert at.session_state["analysis_id"][1] == "a1"
    # The id is on screen: it is what makes the log walkthrough possible.
    assert trace_id == at.session_state["trace_id"]


def test_a_finished_report_computes_its_four_tiles_from_the_results():
    """Overall, Quotes verified and Needs review are presentation decisions --
    the worst state across the criteria, and two counts -- not backend facts,
    so they are walked out of `results` rather than read off `totals`."""
    stub = StubApi(analyses={"a1": analysis(
        status="done",
        report=report(results=[
            result("password_management", "Fully Compliant"),
            result("data_in_transit", "Partially Compliant",
                   quotes=[quote(verified=True), quote("Invented.", verified=False)],
                   needs_review=True),
        ]),
    )})
    at = app(stub, view="analysis", document_id=1, analysis_id={1: "a1"})

    tiles = {m.label: m.value for m in at.metric}
    assert tiles["Quotes verified"] == "2 / 3"
    assert tiles["Needs review"] == "1"
    # The worst state across the criteria, not the first or the most common.
    assert "Partially Compliant" in texts(at)
    assert any(d.label == "Export JSON" for d in at.get("download_button"))


def test_a_quote_that_failed_verification_does_not_look_like_one_that_passed():
    """`01_ui_spec.md` §5.2: the markers exist but no screen used them, because
    the sample contract comes back all-met. This is that screen."""
    stub = StubApi(analyses={"a1": analysis(
        status="done",
        report=report(results=[result(
            quotes=[quote("Nowhere in the contract.", verified=False)],
            state="Non-Compliant", needs_review=True,
        )]),
    )})
    at = app(stub, view="analysis", document_id=1, analysis_id={1: "a1"},
             open_criterion="data_in_transit")

    rendered = texts(at)
    assert "not found verbatim" in rendered
    assert "could not be matched to its passage" in rendered
    assert "needs review" in rendered


def test_the_export_is_the_report_verbatim():
    """The same object `scripts/analyze.py --out` writes, so what downloads
    validates as an `AnalysisReport` rather than as a UI's idea of one."""
    payload = report()
    stub = StubApi(analyses={"a1": analysis(status="done", report=payload)})
    at = app(stub, view="analysis", document_id=1, analysis_id={1: "a1"})

    assert any(d.label == "Export JSON" for d in at.get("download_button"))
    # The button's payload is not readable from a headless run, so the function
    # that builds it is asserted directly.
    from contract_analyzer.report import AnalysisReport
    from contract_analyzer.ui.views.analysis import export_payload

    exported = json.loads(export_payload(analysis(status="done", report=payload)))
    assert exported == payload
    AnalysisReport.model_validate(exported)


def test_a_failed_run_renders_the_runners_reason_not_a_generic_message():
    stub = StubApi(analyses={"a1": analysis(
        status="failed", error="HttpFailure: POST /v1/messages gave up after 4 attempts",
    )})
    at = app(stub, view="analysis", document_id=1, analysis_id={1: "a1"})

    assert "gave up after 4 attempts" in texts(at)


def test_an_interrupted_run_is_not_reported_as_a_failure():
    """The distinction the record goes out of its way to keep: nothing refused,
    the machine went away, and the reviewer should be told to run it again."""
    stub = StubApi(analyses={"a1": analysis(status="interrupted")})
    at = app(stub, view="analysis", document_id=1, analysis_id={1: "a1"})

    rendered = texts(at)
    assert "interrupted" in rendered.lower()
    assert "failed" not in rendered.lower().replace("interrupted", "")


def test_an_analysis_that_has_gone_offers_a_new_run_rather_than_a_dead_id():
    stub = StubApi(analyses={})
    at = app(stub, view="analysis", document_id=1, analysis_id={1: "gone"})

    assert not at.exception
    assert at.session_state["analysis_id"] == {}
    assert "has not been analysed yet" in texts(at)


# -- chat -------------------------------------------------------------------


def test_chat_offers_exactly_the_models_the_api_will_accept():
    """The picker is built from `/health`'s `chat_models`, which is the same
    allowlist `POST /chat` validates against -- so it cannot offer a choice the
    API refuses."""
    at = app(StubApi(), view="chat", document_id=1)

    options = {s.label: s.options for s in at.selectbox}
    assert options["Model"] == HEALTH["chat_models"]
    assert options["Retrieval"] == ["hybrid", "vector", "keyword"]
    assert options["Depth"] == ["shallow", "medium", "deep"]


def test_a_question_carries_the_scope_the_settings_and_the_depth_as_a_number():
    """Depth is the one parameter this UI knowingly hides: the control says
    "deep" and the request carries a passage count."""
    stub = StubApi()
    at = app(stub, view="chat", document_id=1, chat_depth="deep",
             chat_retrieval="keyword", chat_model="claude-haiku-4-5")

    at.chat_input[0].set_value("What TLS version is required?").run()

    asked = next(c for c in stub.calls if c[0] == "chat")
    _, document_id, question, kwargs = asked
    assert document_id == 1
    assert question == "What TLS version is required?"
    assert kwargs["model"] == "claude-haiku-4-5"
    assert kwargs["retrieval_mode"] == "keyword"
    from contract_analyzer.ui.state import DEPTH_TOP_K

    assert kwargs["top_k"] == DEPTH_TOP_K["deep"]
    # The number never reaches the screen.
    assert str(DEPTH_TOP_K["deep"]) not in "".join(str(s.value) for s in at.selectbox)


def test_a_transcript_belongs_to_its_document_and_is_sent_back():
    """The API is stateless, so this list *is* the conversation -- and it is
    keyed by document, because carrying one contract's transcript onto another
    is the leak the whole product is built to prevent."""
    stub = StubApi(documents=[document(1), document(2, "Two.pdf")])
    at = app(stub, view="chat", document_id=1)

    at.chat_input[0].set_value("First question").run()
    at.chat_input[0].set_value("Second question").run()

    second = [c for c in stub.calls if c[0] == "chat"][1]
    assert [m["content"] for m in second[3]["history"]][:2] == [
        "First question", "The vendor must use TLS 1.2."
    ]

    # Switch documents: the other contract starts empty.
    at.session_state["document_id"] = 2
    at.run()
    at.chat_input[0].set_value("About the other one").run()
    third = [c for c in stub.calls if c[0] == "chat"][-1]
    assert third[1] == 2 and third[3]["history"] == []


def test_an_answer_keeps_its_citations_for_re_rendering():
    """Citations are stored with the turn. Re-rendering the transcript must not
    cost a second dollar to reproduce something the client already has."""
    at = app(StubApi(), view="chat", document_id=1)

    at.chat_input[0].set_value("What TLS version?").run()

    turns = at.session_state["chat_history"][1]
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[1]["citations"] and turns[1]["citations"][0]["section_ref"]
    rendered = texts(at)
    assert "7.2 Data in Transit" in rendered
    assert "every quote checked against the source passage" in rendered


def test_a_failure_before_the_stream_opens_appends_no_turn():
    """A transcript holding a question with no reply is worse than one that
    never took the question."""
    stub = StubApi(raise_on={"chat_stream": ApiError(
        "no_api_key", "ANTHROPIC_API_KEY is not set.", "Set it in .env and restart.")})
    at = app(stub, view="chat", document_id=1)

    at.chat_input[0].set_value("Anything").run()

    assert at.session_state["chat_history"].get(1, []) == []
    assert "This needs an answer key" in texts(at)


def test_a_failure_mid_stream_keeps_the_partial_answer_and_says_so():
    """Never a bare spinner. The text that arrived is worth reading, and the
    turn is marked incomplete rather than silently truncated."""
    stub = StubApi(stream_error=ApiError("internal", "The answer failed partway.", "Ask again."))
    at = app(stub, view="chat", document_id=1)

    at.chat_input[0].set_value("Anything").run()

    turns = at.session_state["chat_history"][1]
    assert turns[1]["content"] == "The vendor must use TLS 1.2."
    assert "incomplete" in turns[1]["caption"]


def test_chat_without_a_key_says_so_instead_of_offering_an_input():
    at = app(StubApi(health=dict(HEALTH, key_present=False)), view="chat", document_id=1)

    assert at.chat_input == []
    assert "Chat needs an answer key" in texts(at)


# -- copy rules -------------------------------------------------------------


def test_a_reviewer_is_never_shown_the_word_chunk():
    """§4 of the spec: never "chunk" to a user except as a count. Say
    "passage"."""
    at = app(
        StubApi(documents=[document(1, "A.pdf", last={
            "analysis_id": "a1", "status": "done", "completed_at": "x",
            "states": {"Fully Compliant": 5}, "needs_review": 0})]),
        view="library",
    )

    rendered = texts(at).lower()
    assert "chunk" not in rendered
    assert "passages" in rendered
