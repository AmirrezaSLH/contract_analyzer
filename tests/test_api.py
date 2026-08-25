"""The HTTP API, offline: no network, no key, no real model.

The suite is built around the claims that would be expensive to discover in a
demo, and each one is falsifiable here:

* **An upload cannot escape `RAW_DIR`.** `filename` is client-controlled and
  `../../../evil.pdf` is a path; the test asserts that nothing is written
  outside the upload directory whatever the client sends, and that the name the
  client sent still comes back in the listing, as data.
* **A failed ingest is not a 201.** `ingest_file` reports rather than raises, so
  the route branches on its status; a missing embedding key must be `503
  embedder_unavailable` and not a document with zero chunks.
* **Two documents cannot see each other.** The whole point of `document_id`
  everywhere. Analysis and chat on A must never quote B, and the decoy corpus is
  what makes that falsifiable rather than assumed.
* **A job is a job.** `POST /analyses` returns in milliseconds, polling shows
  progress, the stream can be joined late, and a duplicate submission does not
  spend a second dollar.
* **One trace id runs through everything**, request header to the tool calls
  five criteria deep.

`analysis_workers=1` throughout: `ScriptedAPI` pops its outcomes off a list with
no lock, so parallel criteria have no deterministic order. That is a property of
the test harness, not of the runner -- `tests/test_report.py` covers the pool.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import (
    ScriptedAPI,
    critic_turn,
    make_chunk,
    scripted_client,
    sse_message,
    write_decoy_contract,
)
from contract_analyzer.analyses import get_analysis, queue_analysis
from contract_analyzer.api.main import create_app
from contract_analyzer.compliance import get_criteria
from contract_analyzer.config import Settings
from contract_analyzer.db import get_db
from contract_analyzer.embeddings.fake import FakeEmbedder
from contract_analyzer.generation import tools as T

#: A directory with no `index.html` in it, so the app under test serves the API
#: and nothing else. The front-end bundle is a build artefact: whether one is
#: present in this checkout depends on whether anyone has run `make ui-build`,
#: and a suite whose behaviour turns on that is a suite that passes on one
#: machine and fails on the next. `test_ui_serving.py` supplies its own bundle.
NO_BUNDLE = Path(__file__).resolve().parent / "no-such-bundle"

CRITERIA = get_criteria()
CLAUSE = "Supplier shall rotate credentials every ninety (90) days and encrypt data in transit."
DECOY_CLAUSE = "Zephyrine Holdings shall vault every privileged password without exception."
PDF_HEADER = b"%PDF-1.4\n"


# --------------------------------------------------------------------------
# The app under test
# --------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        anthropic_api_key="k",
        embedding_provider="fake",
        embedding_model=None,
        embedding_dim=64,
        db_path=tmp_path / "contracts.db",
        raw_dir=tmp_path / "raw",
        assets_dir=tmp_path / "assets",
        log_file=None,
        analysis_workers=1,
        api_workers=2,
        analysis_max_tool_calls=4,
        structure_fix_rounds=0,
        api_max_upload_mb=1,
        api_keepalive_seconds=60,
    )


@pytest.fixture
def api() -> ScriptedAPI:
    """The model. Tests push outcomes onto it before making a request."""
    return ScriptedAPI()


@pytest.fixture
def client(settings, api):
    app = create_app(settings, embedder=FakeEmbedder(settings),
                     client=scripted_client(api), static_dir=NO_BUNDLE)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def keyless(settings):
    """The same app with no answer key: upload works, analysis and chat do not."""
    app = create_app(
        settings.model_copy(update={"anthropic_api_key": None}),
        embedder=FakeEmbedder(settings),
        client=None,
        static_dir=NO_BUNDLE,
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def searches(monkeypatch):
    """Retrieval returns one clause from whichever document was asked for, so a
    leak across documents would show up as the *other* document's text."""
    from contract_analyzer.retrieval.base import RetrievalResult

    def retrieve(question, conn, embedder=None, settings=None, *, document_id, mode=None,
                 top_k=None, candidates=None):
        text = CLAUSE if document_id == 1 else DECOY_CLAUSE
        return RetrievalResult(
            question=question, mode=mode or "hybrid", document_id=document_id,
            chunks=[make_chunk(document_id, text, document_id=document_id)],
            candidates=20, top_k=top_k or 6,
        )

    monkeypatch.setattr(T, "retrieve", retrieve)


@pytest.fixture(scope="session")
def small_pdf(tmp_path_factory) -> bytes:
    """A two-page synthetic contract, built once.

    Most tests here need *a* document, not *the* document: they are about
    status codes, job states and streams. Parsing the 21-page sample for each
    of them costs about a second a test, and `make test` runs before every
    commit. The tests that are actually about the sample's content ask for it
    by name.
    """
    pytest.importorskip("pymupdf")
    path = write_decoy_contract(tmp_path_factory.mktemp("pdf") / "small.pdf")
    return path.read_bytes()


@pytest.fixture(scope="session")
def sample_pdf() -> bytes:
    """The real contract, for the assertions that are about its content."""
    path = Path(__file__).resolve().parents[1] / "data" / "samples" / "Sample Contract.pdf"
    if not path.exists():
        pytest.skip("sample contract not present")
    return path.read_bytes()


def upload(client, body, name="Sample Contract.pdf"):
    return client.post("/api/documents", files={"file": (name, body, "application/pdf")})


def escaped(tmp_path, settings) -> list[str]:
    """Anything the upload wrote outside `raw_dir`.

    The database and its `-wal` / `-shm` sidecars are not that: WAL sidecars
    exist for as long as a connection is open, and the metrics store's writer
    holds one for the life of the app.
    """
    return sorted(
        entry.name
        for entry in tmp_path.iterdir()
        if entry != settings.raw_dir and not entry.name.startswith("contracts.db")
    )


# --------------------------------------------------------------------------
# The script
# --------------------------------------------------------------------------


def draft_for(criterion, quote="rotate credentials") -> str:
    return json.dumps({
        "compliance_question": criterion.question,
        "compliance_state": "Fully Compliant",
        "sub_requirements": [
            {"id": s.id, "requirement": s.requirement, "status": "met", "quote_indexes": [0]}
            for s in criterion.sub_requirements
        ],
        "relevant_quotes": [{"text": quote, "evidence_id": "E1"}],
        "rationale": "The clause is explicit.",
        "raw_confidence": 0.9,
    })


def analysis_turns(criterion, quote="rotate credentials") -> list[str]:
    """Search, stop, draft, and the critic's findings on the draft -- the four
    requests one criterion makes now that every result is evaluated."""
    return [
        sse_message([{"type": "tool_use", "id": "t1", "name": "search_contract",
                      "input": {"query": criterion.requirement}}], stop_reason="tool_use"),
        sse_message([{"type": "text", "text": "Enough."}]),
        sse_message([{"type": "text", "text": draft_for(criterion, quote)}]),
        critic_turn(criterion),
    ]


def full_analysis(quote="rotate credentials") -> list[str]:
    return [turn for c in CRITERIA for turn in analysis_turns(c, quote)]


def chat_turns(answer="Credentials rotate every ninety days.", quote="rotate credentials"):
    """A search, then a cited answer. The citation is what the real API would
    extract from the document block we sent."""
    return [
        sse_message([{"type": "tool_use", "id": "t1", "name": "search_contract",
                      "input": {"query": "rotation"}}], stop_reason="tool_use"),
        sse_message([{"type": "text", "text": "Enough."}]),
        sse_message([{
            "type": "text", "text": answer,
            "citations": [{
                "type": "char_location", "document_index": 0, "document_title": "6.6",
                "cited_text": quote, "start_char_index": 0, "end_char_index": len(quote),
            }],
        }]),
    ]


def poll(client, analysis_id, until=("done", "failed", "cancelled"), tries=200):
    for _ in range(tries):
        body = client.get(f"/api/analyses/{analysis_id}").json()
        if body["status"] in until:
            return body
        threading.Event().wait(0.01)
    raise AssertionError(f"analysis stayed {body['status']}")


def events_of(response) -> list[tuple[str, dict]]:
    """Parse an SSE body into (event, data) pairs, ignoring keepalive comments."""
    out = []
    name = None
    for line in response.text.splitlines():
        if line.startswith("event:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and name:
            out.append((name, json.loads(line.split(":", 1)[1].strip())))
            name = None
    return out


# --------------------------------------------------------------------------
# Health and reference
# --------------------------------------------------------------------------


def test_health_reports_what_the_service_can_actually_do(client, keyless):
    body = client.get("/api/health").json()
    assert body["status"] == "ok" and body["db"] is True
    assert body["embedder"] == "fake" and body["key_present"] is True
    assert body["auth_required"] is False
    assert body["analysis_model"] == "claude-sonnet-5"

    # The question a UI actually asks: upload works, analysis does not.
    assert keyless.get("/api/health").json()["key_present"] is False


def test_criteria_publishes_the_sub_requirements(client):
    body = client.get("/api/criteria").json()
    assert len(body) == 5
    assert all(c["sub_requirements"] for c in body)
    assert {c["id"] for c in body} == {c.id for c in CRITERIA}
    assert "Fully Compliant" in body[0]["states"]


# --------------------------------------------------------------------------
# Upload: the parts that handle bytes from outside
# --------------------------------------------------------------------------


def test_upload_indexes_the_contract_and_returns_an_id(client, sample_pdf):
    body = upload(client, sample_pdf).json()
    assert body["document_id"] >= 1
    assert body["filename"] == "Sample Contract.pdf"
    assert body["pages"] == 21 and body["chunks"] > 50
    assert body["spine_source"] == "headings"


def test_the_same_bytes_twice_are_two_documents(client, small_pdf):
    """Sessions are isolated by construction: `ingest_file` keys uniqueness on
    path, and every upload gets a fresh one."""
    first, second = upload(client, small_pdf).json(), upload(client, small_pdf).json()
    assert first["document_id"] != second["document_id"]
    listed = client.get("/api/documents").json()
    assert [d["document_id"] for d in listed] == [second["document_id"], first["document_id"]]


def test_a_traversing_filename_cannot_write_outside_raw_dir(client, settings, tmp_path):
    body = upload(client, PDF_HEADER + b"not a contract", name="../../../evil.pdf").json()

    # The upload is rejected by the parser, or accepted -- either way the only
    # thing that matters is where the bytes went.
    assert not (tmp_path / "evil.pdf").exists()
    assert escaped(tmp_path, settings) == []
    written = list((settings.raw_dir).iterdir())
    assert all(p.parent == settings.raw_dir for p in written)
    assert all(".." not in p.name and "/" not in p.name for p in written)
    if "document_id" in body:
        # The name the client sent survives as *data*, which is what a list has
        # to show; it just never became a path.
        assert body["filename"] == "../../../evil.pdf"


@pytest.mark.parametrize("name", ["..%2f..%2fx.pdf", "....//x.pdf", "/etc/passwd.pdf", ".pdf"])
def test_hostile_filenames_all_land_inside_raw_dir(client, settings, tmp_path, name):
    upload(client, PDF_HEADER + b"x", name=name)
    assert escaped(tmp_path, settings) == []
    assert all(p.parent == settings.raw_dir for p in settings.raw_dir.iterdir())


def test_a_non_pdf_is_refused_before_it_is_stored(client, settings):
    response = client.post("/api/documents", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"
    assert list(settings.raw_dir.iterdir()) == []


def test_an_empty_upload_is_refused(client, settings):
    response = client.post("/api/documents", files={"file": ("x.pdf", b"", "application/pdf")})
    assert response.status_code == 422
    assert list(settings.raw_dir.iterdir()) == []


def test_an_oversize_upload_is_413_and_leaves_no_partial_file(client, settings):
    """Enforced while the body streams: the cap is 1 MB here and the body is 2,
    so a handler that read it whole would have had it in memory already."""
    response = client.post(
        "/api/documents", files={"file": ("big.pdf", PDF_HEADER + b"x" * (2 * 1024 * 1024),
                                      "application/pdf")}
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert list(settings.raw_dir.iterdir()) == []


def test_a_missing_embedding_key_is_503_not_a_201_with_no_chunks(settings, tmp_path):
    """`ingest_file` returns its failure rather than raising, so a handler that
    only mapped exceptions would answer 201 with an empty document."""
    openai_settings = settings.model_copy(
        update={"embedding_provider": "openai", "openai_api_key": None}
    )
    app = create_app(openai_settings, embedder=None, client=None, static_dir=NO_BUNDLE)
    with TestClient(app) as client:
        response = upload(client, PDF_HEADER + b"x")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "embedder_unavailable"
    assert "embedding" in response.json()["error"]["hint"].lower()


# --------------------------------------------------------------------------
# The catalogue over HTTP
# --------------------------------------------------------------------------


def test_sections_are_the_outline_a_picker_shows(client, sample_pdf):
    document_id = upload(client, sample_pdf).json()["document_id"]
    sections = client.get(f"/api/documents/{document_id}/sections").json()
    assert len(sections) > 20
    assert all(s["chunks"] >= 1 for s in sections)
    assert any(s["title"].startswith("6.6") for s in sections)


def test_unknown_ids_are_404_with_a_hint_that_names_an_action(client):
    """The hint has two readers -- a model recovering from a tool call, and a
    person reading the second line of an error surface -- so it names what to
    do rather than which route to call. `code` is the machine-readable half."""
    for path in ("/api/documents/999", "/api/documents/999/sections", "/api/analyses/nope"):
        error = client.get(path).json()["error"]
        assert error["code"] in ("document_not_found", "analysis_not_found"), path
        hint = error["hint"]
        assert hint and hint[0].isupper() and hint.endswith("."), path
        # A route spelling instead of an action is the thing this asserts against.
        assert "GET /" not in hint and "POST /" not in hint, path


def test_the_library_row_carries_its_last_analysis(client, api, searches, small_pdf):
    """`GET /documents` is what a library table renders from, so the outcome of
    the newest analysis travels with the row rather than costing a request per
    document. `states` is a count per compliance state, not a summary
    sentence: the words are the client's to choose."""
    document_id = upload(client, small_pdf).json()["document_id"]

    fresh = client.get("/api/documents").json()[0]
    assert fresh["document_id"] == document_id
    assert fresh["pages"] and fresh["chunks"]
    # Never analysed is `null`, which is what draws "Not analysed".
    assert fresh["last_analysis"] is None

    api.outcomes.extend(full_analysis())
    analysis_id = client.post(
        "/api/analyses", json={"document_id": document_id}
    ).json()["analysis_id"]
    poll(client, analysis_id)

    last = client.get("/api/documents").json()[0]["last_analysis"]
    assert last["analysis_id"] == analysis_id and last["status"] == "done"
    assert sum(last["states"].values()) == len(CRITERIA)
    # The same projection on the detail endpoint, from the same query.
    assert client.get(f"/api/documents/{document_id}").json()["last_analysis"] == last


def test_the_last_analysis_is_the_newest_one(client, api, searches, small_pdf):
    """Two runs against one document: the row shows the second."""
    document_id = upload(client, small_pdf).json()["document_id"]
    api.outcomes.extend(full_analysis())
    first = client.post("/api/analyses", json={"document_id": document_id}).json()["analysis_id"]
    poll(client, first)

    api.outcomes.extend(full_analysis())
    second = client.post(
        "/api/analyses", json={"document_id": document_id}, headers={"Idempotency-Key": "again"}
    ).json()["analysis_id"]
    poll(client, second)

    assert second != first
    assert client.get("/api/documents").json()[0]["last_analysis"]["analysis_id"] == second


def test_delete_removes_the_document_and_its_file(client, settings, small_pdf):
    document_id = upload(client, small_pdf).json()["document_id"]
    assert len(list(settings.raw_dir.iterdir())) == 1

    assert client.delete(f"/api/documents/{document_id}").status_code == 204
    assert client.get(f"/api/documents/{document_id}").status_code == 404
    assert list(settings.raw_dir.iterdir()) == []


def test_delete_is_refused_while_an_analysis_is_running(client, api, searches, small_pdf):
    document_id = upload(client, small_pdf).json()["document_id"]
    api.outcomes.extend(full_analysis())
    analysis_id = client.post(
        "/api/analyses", json={"document_id": document_id}
    ).json()["analysis_id"]

    # The job may already have finished; only assert the conflict if it has not.
    response = client.delete(f"/api/documents/{document_id}")
    if response.status_code == 409:
        assert response.json()["error"]["code"] == "analysis_running"
    poll(client, analysis_id)
    assert client.delete(f"/api/documents/{document_id}").status_code == 204


# --------------------------------------------------------------------------
# Retrieval over HTTP
# --------------------------------------------------------------------------


def test_search_returns_passages_a_reviewer_could_check(client, sample_pdf):
    """The endpoint a host with its own model calls instead of `POST /chat`.

    Every field asserted here is one a reader needs to find the clause: the
    section it came from, the printed page, and the chunk id that is the same
    handle a report's quote carries.
    """
    document_id = upload(client, sample_pdf).json()["document_id"]
    body = client.post(
        f"/api/documents/{document_id}/search",
        json={"query": "password rotation and complexity"},
    ).json()

    assert body["document_id"] == document_id
    assert body["mode"] == "hybrid"
    assert body["passages"]
    for passage in body["passages"]:
        assert passage["text"].strip()
        assert isinstance(passage["chunk_id"], int)
        # A breadcrumb, not just a page: "6.6 Password Management Standard" is
        # what makes a citation checkable in seconds.
        assert passage["breadcrumb"] or passage["section"]


def test_search_caps_what_it_returns(client, sample_pdf):
    document_id = upload(client, sample_pdf).json()["document_id"]
    url = f"/api/documents/{document_id}/search"
    assert len(client.post(url, json={"query": "password", "top_k": 2}).json()["passages"]) == 2
    # The cap is the API's, not the caller's: an unbounded top_k is a request
    # to put the whole contract in a model's context window.
    assert client.post(url, json={"query": "password", "top_k": 50}).status_code == 422
    assert client.post(url, json={"query": ""}).status_code == 422


def test_search_falls_back_to_keyword_with_no_embedder(client, settings, small_pdf):
    """A deployment with no embedding key answers, and says which mode it used.

    The alternative is a 503 to a caller that had no way to know which modes
    this deployment can serve -- and keyword retrieval over identifier-heavy
    contract text is a genuine fallback, not a degraded one.
    """
    document_id = upload(client, small_pdf).json()["document_id"]
    keyless_embedder = settings.model_copy(
        update={"embedding_provider": "openai", "openai_api_key": None}
    )
    app = create_app(keyless_embedder, embedder=None, client=None, static_dir=NO_BUNDLE)
    with TestClient(app) as no_vectors:
        body = no_vectors.post(
            f"/api/documents/{document_id}/search", json={"query": "password rotation"}
        ).json()

    assert body["mode"] == "keyword"
    assert body["passages"]


def test_search_on_an_unknown_document_is_404(client):
    error = client.post("/api/documents/999/search", json={"query": "x"}).json()["error"]
    assert error["code"] == "document_not_found"


# --------------------------------------------------------------------------
# Analyses as jobs
# --------------------------------------------------------------------------


def test_an_analysis_is_queued_and_polled_to_a_report(client, api, searches, small_pdf):
    document_id = upload(client, small_pdf).json()["document_id"]
    api.outcomes.extend(full_analysis())

    accepted = client.post("/api/analyses", json={"document_id": document_id})
    assert accepted.status_code == 202
    queued = accepted.json()
    assert queued["status"] in ("queued", "running")
    assert queued["progress"]["total"] == 5
    assert [c["id"] for c in queued["criteria"]] == [c.id for c in CRITERIA]

    done = poll(client, queued["analysis_id"])
    assert done["status"] == "done"
    assert done["progress"] == {"done": 5, "total": 5}
    report = done["report"]
    assert [r["criterion_id"] for r in report["results"]] == [c.id for c in CRITERIA]
    assert all(r["compliance_state"] == "Fully Compliant" for r in report["results"])
    assert all(q["verified"] for r in report["results"] for q in r["relevant_quotes"])
    assert report["totals"]["criteria"] == 5 and report["totals"]["cost_usd"] > 0


def test_summary_detail_drops_the_bulky_fields(client, api, searches, small_pdf):
    document_id = upload(client, small_pdf).json()["document_id"]
    api.outcomes.extend(full_analysis())
    analysis_id = client.post(
        "/api/analyses", json={"document_id": document_id}
    ).json()["analysis_id"]
    poll(client, analysis_id)

    summary = client.get(f"/api/analyses/{analysis_id}", params={"detail": "summary"}).json()
    assert all(r["relevant_quotes"] == [] and r["rationale"] == ""
               for r in summary["report"]["results"])
    # And the stored report is untouched for the next reader.
    full = client.get(f"/api/analyses/{analysis_id}").json()
    assert full["report"]["results"][0]["relevant_quotes"]


def test_a_duplicate_submission_joins_the_running_analysis(client, api, searches, small_pdf):
    """At roughly a dollar a run, a double-clicked button must not be two runs."""
    document_id = upload(client, small_pdf).json()["document_id"]
    api.outcomes.extend(full_analysis())

    first = client.post("/api/analyses", json={"document_id": document_id})
    second = client.post("/api/analyses", json={"document_id": document_id})

    assert first.status_code == 202
    if first.json()["status"] in ("queued", "running"):
        assert second.status_code == 200
        assert second.json()["analysis_id"] == first.json()["analysis_id"]
    poll(client, first.json()["analysis_id"])
    assert len(client.get("/api/analyses", params={"document_id": document_id}).json()) == 1


def test_an_idempotency_key_forces_a_second_run(client, api, searches, small_pdf):
    document_id = upload(client, small_pdf).json()["document_id"]
    api.outcomes.extend(full_analysis() * 2)

    first = client.post("/api/analyses", json={"document_id": document_id})
    second = client.post(
        "/api/analyses", json={"document_id": document_id}, headers={"Idempotency-Key": "again"}
    )
    assert second.status_code == 202
    assert second.json()["analysis_id"] != first.json()["analysis_id"]


def test_the_declared_surface_is_recorded_on_the_run(client, api, searches, small_pdf):
    """Without this every HTTP caller is `api` and the KPI slice is one bucket."""
    document_id = upload(client, small_pdf).json()["document_id"]
    api.outcomes.extend(full_analysis())
    accepted = client.post(
        "/api/analyses", json={"document_id": document_id}, headers={"X-Surface": "mcp"}
    )
    analysis_id = accepted.json()["analysis_id"]
    poll(client, analysis_id)

    conn = get_db(client.app.state.settings)
    try:
        assert get_analysis(conn, analysis_id).surface == "mcp"
    finally:
        conn.close()


def test_an_unrecorded_surface_is_refused_rather_than_coerced(client, small_pdf):
    """Falling back to `api` would file the run in a bucket the caller does not
    believe it is in, and a KPI split nobody can reproduce is worse than none."""
    document_id = upload(client, small_pdf).json()["document_id"]
    response = client.post(
        "/api/analyses", json={"document_id": document_id}, headers={"X-Surface": "whatever"}
    )
    assert response.status_code == 422
    assert "mcp" in response.json()["error"]["hint"]


def test_a_bad_submission_is_rejected_before_anything_is_queued(client, api, small_pdf):
    document_id = upload(client, small_pdf).json()["document_id"]

    assert client.post("/api/analyses", json={}).status_code == 422
    unknown = client.post("/api/analyses", json={"document_id": 999})
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "document_not_found"

    bad = client.post("/api/analyses", json={"document_id": document_id, "criteria": ["nope"]})
    assert bad.status_code == 422
    assert "criterion ids" in bad.json()["error"]["hint"]

    assert api.calls == 0
    assert client.get("/api/analyses", params={"document_id": document_id}).json() == []


def test_without_a_key_no_job_is_queued_at_all(keyless, small_pdf):
    """A 202 followed by a job that fails immediately is a worse answer than an
    error, so the key is checked before the submission is accepted."""
    document_id = upload(keyless, small_pdf).json()["document_id"]

    response = keyless.post("/api/analyses", json={"document_id": document_id})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "no_api_key"
    assert keyless.get("/api/analyses", params={"document_id": document_id}).json() == []
    assert keyless.post("/api/chat", json={"document_id": document_id, "question": "?"})\
        .status_code == 503


def test_a_subset_of_criteria_can_be_analysed(client, api, searches, small_pdf):
    document_id = upload(client, small_pdf).json()["document_id"]
    api.outcomes.extend(analysis_turns(CRITERIA[0]))

    queued = client.post(
        "/api/analyses", json={"document_id": document_id, "criteria": [CRITERIA[0].id]}
    ).json()
    assert queued["progress"]["total"] == 1
    done = poll(client, queued["analysis_id"])
    assert [r["criterion_id"] for r in done["report"]["results"]] == [CRITERIA[0].id]


def test_cancelling_leaves_a_partial_report(client, api, searches, small_pdf):
    document_id = upload(client, small_pdf).json()["document_id"]
    api.outcomes.extend(full_analysis())
    analysis_id = client.post(
        "/api/analyses", json={"document_id": document_id}
    ).json()["analysis_id"]

    client.post(f"/api/analyses/{analysis_id}/cancel")
    done = poll(client, analysis_id)
    assert done["status"] == "cancelled"
    assert len(done["report"]["results"]) + len(done["report"]["skipped"]) == 5

    again = client.post(f"/api/analyses/{analysis_id}/cancel")
    assert again.status_code == 409 and again.json()["error"]["code"] == "not_running"


def test_a_failing_model_fails_the_job_rather_than_hanging_it(client, api, searches, small_pdf):
    document_id = upload(client, small_pdf).json()["document_id"]
    api.outcomes.append(500)

    analysis_id = client.post(
        "/api/analyses", json={"document_id": document_id}
    ).json()["analysis_id"]
    done = poll(client, analysis_id)
    assert done["status"] == "failed" and done["error"]
    assert done["report"] is None



# --------------------------------------------------------------------------
# Durability: what is still there after the process is not
# --------------------------------------------------------------------------


def restarted(settings, api) -> TestClient:
    """A second app over the same database. What a restart looks like from the
    outside, and the only way to falsify "the report survived it"."""
    return TestClient(create_app(settings, embedder=FakeEmbedder(settings),
                                 client=scripted_client(api), static_dir=NO_BUNDLE))


def analysed(client, api, small_pdf) -> tuple[int, str, dict]:
    """Upload, analyse, poll to `done`. Returns what the three tests below need."""
    document_id = upload(client, small_pdf).json()["document_id"]
    api.outcomes.extend(full_analysis())
    analysis_id = client.post(
        "/api/analyses", json={"document_id": document_id}
    ).json()["analysis_id"]
    return document_id, analysis_id, poll(client, analysis_id)


def test_an_analysis_and_its_report_survive_a_restart(client, api, settings, searches, small_pdf):
    """The bug this table exists for: before it, the second process answered
    404 and the report was gone."""
    document_id, analysis_id, done = analysed(client, api, small_pdf)

    with restarted(settings, api) as fresh:
        body = fresh.get(f"/api/analyses/{analysis_id}").json()
        assert body["status"] == "done"
        assert body["report"] == done["report"]
        assert body["totals"] == done["totals"]
        assert body["trace_id"] == done["trace_id"]
        assert [c["id"] for c in body["criteria"]] == [c.id for c in CRITERIA]

        listed = fresh.get("/api/analyses", params={"document_id": document_id}).json()
        assert [a["analysis_id"] for a in listed] == [analysis_id]
        detail = fresh.get(f"/api/documents/{document_id}").json()
        assert [a["analysis_id"] for a in detail["analyses"]] == [analysis_id]


def test_a_run_the_process_died_holding_reads_interrupted(client, api, settings, small_pdf):
    """`failed` would say the model refused. It did not -- the machine went
    away -- and the client is owed the difference."""
    document_id = upload(client, small_pdf).json()["document_id"]
    conn = get_db(settings)
    queue_analysis(conn, "orphan", document_id, filename="x.pdf",
                   criteria=[c.id for c in CRITERIA])
    conn.execute("UPDATE analyses SET status = 'running' WHERE analysis_id = 'orphan'")
    conn.commit()
    conn.close()

    with restarted(settings, api) as fresh:
        body = fresh.get("/api/analyses/orphan").json()
        assert body["status"] == "interrupted"
        assert body["report"] is None and body["progress"]["total"] == len(CRITERIA)

        # And an id that really is unknown no longer apologises about restarts.
        hint = fresh.get("/api/analyses/nosuchid").json()["error"]["hint"]
        assert "restart" not in hint


def test_the_live_job_wins_where_both_have_an_answer(client, api, settings, searches, small_pdf):
    """The dict is the live half and the row is the durable one. The process
    running an analysis answers from the dict, which is why a row rewritten
    underneath it does not change what it says -- and a process that never ran
    it has only the row."""
    _, analysis_id, _ = analysed(client, api, small_pdf)

    conn = get_db(settings)
    conn.execute(
        "UPDATE analyses SET status = 'interrupted', report_json = NULL WHERE analysis_id = ?",
        (analysis_id,),
    )
    conn.commit()
    conn.close()

    live = client.get(f"/api/analyses/{analysis_id}").json()
    assert live["status"] == "done" and live["report"] is not None

    with restarted(settings, api) as fresh:
        assert fresh.get(f"/api/analyses/{analysis_id}").json()["status"] == "interrupted"


def test_deleting_the_contract_leaves_the_analysis_and_its_report(
    client, api, settings, searches, small_pdf
):
    """The report is the deliverable and it is self-contained. Asserted through
    a restart, so it is the *row* that is shown to have survived, not the dict
    still holding it."""
    document_id, analysis_id, done = analysed(client, api, small_pdf)

    assert client.delete(f"/api/documents/{document_id}").status_code == 204
    assert client.get(f"/api/documents/{document_id}").status_code == 404

    with restarted(settings, api) as fresh:
        body = fresh.get(f"/api/analyses/{analysis_id}").json()
        assert body["status"] == "done"
        assert body["report"] == done["report"]

# --------------------------------------------------------------------------
# The event stream
# --------------------------------------------------------------------------


def test_the_stream_reports_every_criterion_and_then_closes(client, api, searches, small_pdf):
    document_id = upload(client, small_pdf).json()["document_id"]
    api.outcomes.extend(full_analysis())
    analysis_id = client.post(
        "/api/analyses", json={"document_id": document_id}
    ).json()["analysis_id"]

    with client.stream("GET", f"/api/analyses/{analysis_id}/events") as response:
        assert response.status_code == 200
        response.read()
    events = events_of(response)

    names = [name for name, _ in events]
    assert names[-1] == "done"
    assert names.count("criterion") == 5
    tool_calls = [data for name, data in events if name == "tool_call"]
    assert len(tool_calls) == 5
    # Every tool call says which criterion made it: five parallel runs would be
    # unattributable otherwise.
    assert {t["criterion"] for t in tool_calls} == {c.id for c in CRITERIA}

    # The critic is a phase a subscriber can see, not a silence in the middle
    # of the run: five `evaluating` events and five `decision` events, each
    # tagged with the criterion whose draft was being read.
    evaluating = [data for name, data in events if name == "evaluating"]
    decisions = [data for name, data in events if name == "decision"]
    assert {e["criterion"] for e in evaluating} == {c.id for c in CRITERIA}
    assert {d["verdict"] for d in decisions} == {"accept"}
    assert "revising" not in names  # nothing was disputed, so nothing was redone


def test_the_verdict_and_the_rounds_reach_the_wire(client, api, searches, small_pdf):
    """A client that only knows `needs_review` cannot say *why* a criterion
    needs one. `verdict` is what distinguishes an accepted result from one that
    ran out of rounds and one the critic never managed to read."""
    document_id = upload(client, small_pdf).json()["document_id"]
    api.outcomes.extend(full_analysis())
    analysis_id = client.post(
        "/api/analyses", json={"document_id": document_id}
    ).json()["analysis_id"]
    body = poll(client, analysis_id)

    assert [c["verdict"] for c in body["criteria"]] == ["accept"] * 5
    assert [c["rounds"] for c in body["criteria"]] == [0] * 5
    assert body["totals"]["accepted"] == 5
    assert body["totals"]["evaluator_cost_usd"] > 0
    result = body["report"]["results"][0]
    assert result["verdict"] == "accept"
    assert result["evaluator_findings"]["state_agreement"] == "agree"
    assert result["confidence_components"]["critic"] == 0.85


def test_subscribing_after_the_job_finished_replays_and_closes(client, api, searches, small_pdf):
    """A stream that hangs until a keepalive gives up is the failure this
    replaces: the client gets what it missed, the terminal event, and EOF."""
    document_id = upload(client, small_pdf).json()["document_id"]
    api.outcomes.extend(full_analysis())
    analysis_id = client.post(
        "/api/analyses", json={"document_id": document_id}
    ).json()["analysis_id"]
    poll(client, analysis_id)

    with client.stream("GET", f"/api/analyses/{analysis_id}/events") as response:
        response.read()
    events = events_of(response)
    assert [name for name, _ in events].count("criterion") == 5
    assert events[-1][0] == "done"
    assert events[-1][1]["status"] == "done"


def test_two_subscribers_both_see_the_whole_run(client, api, searches, small_pdf):
    """One `queue.Queue` per job would let a reconnecting UI steal the first
    stream's events; each subscriber gets its own."""
    document_id = upload(client, small_pdf).json()["document_id"]
    api.outcomes.extend(full_analysis())
    analysis_id = client.post(
        "/api/analyses", json={"document_id": document_id}
    ).json()["analysis_id"]

    collected: list[list[tuple[str, dict]]] = []
    lock = threading.Lock()

    def watch():
        with client.stream("GET", f"/api/analyses/{analysis_id}/events") as response:
            response.read()
        with lock:
            collected.append(events_of(response))

    watchers = [threading.Thread(target=watch) for _ in range(2)]
    for w in watchers:
        w.start()
    for w in watchers:
        w.join(timeout=30)

    assert len(collected) == 2
    for events in collected:
        assert [n for n, _ in events].count("criterion") == 5
        assert events[-1][0] == "done"


def test_the_log_stream_is_published(client):
    """Mounted and documented. The body never closes -- that is the point --
    so the lines themselves are asserted in `test_log_stream.py` against the
    hub, not against a TestClient that would wait on EOF."""
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/logs/events" in paths
    assert "get" in paths["/api/logs/events"]


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------


def test_chat_returns_one_json_body_when_not_streaming(client, api, searches, small_pdf):
    document_id = upload(client, small_pdf).json()["document_id"]
    api.outcomes.extend(chat_turns())

    body = client.post(
        "/api/chat", json={"document_id": document_id, "question": "How often do passwords rotate?",
                       "stream": False}
    ).json()

    assert body["text"] == "Credentials rotate every ninety days."
    assert len(body["citations"]) == 1
    citation = body["citations"][0]
    # `text`, `section_ref` and `verified`, matching `ResolvedQuote`: one card
    # in a client renders a chat citation and a report quote alike.
    assert citation["text"] == "rotate credentials"
    assert set(citation) >= {"text", "section_ref", "verified"}
    assert citation["evidence_id"] == "E1" and citation["chunk_id"] == document_id
    assert body["grounded"] is True and body["tool_calls"] == 1
    assert body["usage"]["input_tokens"] > 0 and body["cost_usd"] > 0


def test_chat_settings_reach_the_run_and_the_answer_reports_them(
    client, api, searches, small_pdf, monkeypatch
):
    """Model, retrieval mode and passage count are per-question overrides. The
    answer reports the model that actually ran, not the one that was asked
    for -- those differ the moment an allowlist or a fallback intervenes."""
    document_id = upload(client, small_pdf).json()["document_id"]
    api.outcomes.extend(chat_turns())

    seen = {}
    import contract_analyzer.generation.tools as tools_module

    original = tools_module.ContractTools.__init__

    def spy(self, *args, **kwargs):
        original(self, *args, **kwargs)
        seen["mode"], seen["top_k"] = self.default_mode, self.default_top_k

    monkeypatch.setattr(tools_module.ContractTools, "__init__", spy)

    body = client.post(
        "/api/chat",
        json={"document_id": document_id, "question": "How often?", "stream": False,
              "model": "claude-haiku-4-5", "retrieval_mode": "keyword", "top_k": 3},
    ).json()

    assert seen == {"mode": "keyword", "top_k": 3}
    assert body["model"] == "claude-haiku-4-5"


def test_chat_refuses_a_model_this_deployment_does_not_offer(client, api, small_pdf):
    """`POST /chat` is open when API_KEY is unset, so a free-text model id is a
    request to spend this deployment's key on whatever the caller names. The
    refusal happens before any request to the provider."""
    document_id = upload(client, small_pdf).json()["document_id"]

    refused = client.post(
        "/api/chat",
        json={"document_id": document_id, "question": "How often?", "stream": False,
              "model": "some-expensive-model"},
    )

    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "validation"
    assert api.calls == 0
    # And the client can find out what it may ask for without guessing.
    assert "claude-haiku-4-5" in client.get("/api/health").json()["chat_models"]


def test_chat_citations_use_the_same_field_names_as_a_report_quote(
    client, api, searches, small_pdf
):
    """One card in a client renders both, so `text`, `section_ref` and
    `verified` mean the same thing on an answer and on a report."""
    document_id = upload(client, small_pdf).json()["document_id"]
    api.outcomes.extend(chat_turns())

    citation = client.post(
        "/api/chat", json={"document_id": document_id, "question": "How often?", "stream": False},
    ).json()["citations"][0]

    from contract_analyzer.compliance.schemas import ResolvedQuote

    shared = {"text", "evidence_id", "section_ref", "page_display", "chunk_id", "verified"}
    assert shared <= set(citation)
    assert shared <= set(ResolvedQuote.model_fields)
    # Extracted by the model API from the block we sent, so it must verify.
    assert citation["verified"] is True


def test_chat_streams_deltas_then_citations_then_done(client, api, searches, small_pdf):
    document_id = upload(client, small_pdf).json()["document_id"]
    api.outcomes.extend(chat_turns())

    with client.stream(
        "POST", "/api/chat", json={"document_id": document_id, "question": "rotation?"}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        response.read()
    events = events_of(response)

    text = "".join(data["text"] for name, data in events if name == "text")
    assert text == "Credentials rotate every ninety days."
    names = [name for name, _ in events]
    assert names.count("citations") == 1 and names[-1] == "done"
    assert names.count("tool_call") == 1
    done = events[-1][1]
    assert done["usage"]["output_tokens"] > 0 and done["grounded"] is True


def test_chat_on_an_unknown_document_is_404_not_an_error_event(client):
    """A 404 has to be a 404: an `error` frame inside a 200 is invisible to
    every client that checks the status code first."""
    response = client.post("/api/chat", json={"document_id": 999, "question": "?"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "document_not_found"


def test_a_failing_model_becomes_an_error_event_and_a_clean_close(client, api, searches, small_pdf):
    document_id = upload(client, small_pdf).json()["document_id"]
    api.outcomes.extend([500, 500, 500, 500])

    with client.stream(
        "POST", "/api/chat", json={"document_id": document_id, "question": "?"}
    ) as response:
        response.read()
    events = events_of(response)
    assert events[-1][0] == "error"
    assert events[-1][1]["code"] in ("upstream_failure", "HttpFailure")


def test_history_is_replayed_from_the_client(client, api, searches, small_pdf):
    """The API keeps no transcript: four consumers share one backend precisely
    because there is no session store between them."""
    document_id = upload(client, small_pdf).json()["document_id"]
    api.outcomes.extend(chat_turns())

    client.post("/api/chat", json={
        "document_id": document_id, "question": "and rotation?", "stream": False,
        "history": [{"role": "user", "content": "what about MFA?"},
                    {"role": "assistant", "content": "Section 6.7 requires it."}],
    })
    sent = json.dumps(api.requests[0])
    assert "what about MFA?" in sent and "Section 6.7 requires it." in sent


# --------------------------------------------------------------------------
# Isolation between documents
# --------------------------------------------------------------------------


def test_chat_on_one_contract_never_quotes_the_other(client, api, searches, small_pdf):
    """`document_id` is threaded to the tools in Python, so the model cannot
    widen its own scope even if it wanted to."""
    first = upload(client, small_pdf).json()["document_id"]
    second = upload(client, small_pdf).json()["document_id"]
    assert first == 1 and second == 2  # the `searches` fixture keys on this

    api.outcomes.extend(chat_turns(answer="Zephyrine vaults passwords.", quote="vault every"))
    body = client.post("/api/chat", json={"document_id": second, "question": "passwords?",
                                      "stream": False}).json()
    assert body["citations"][0]["chunk_id"] == second

    api.outcomes.extend(chat_turns())
    body = client.post("/api/chat", json={"document_id": first, "question": "passwords?",
                                      "stream": False}).json()
    assert body["citations"][0]["chunk_id"] == first
    assert "Zephyrine" not in json.dumps(body)


def test_search_on_one_contract_never_returns_the_other(client, small_pdf):
    """The same invariant as chat and analysis, on the endpoint that has no
    model in front of it to be told about scope: `retrieve` is called with a
    `document_id` and cannot widen it."""
    first = upload(client, small_pdf).json()["document_id"]
    second = upload(client, small_pdf).json()["document_id"]

    def chunks_of(document_id):
        body = client.post(
            f"/api/documents/{document_id}/search", json={"query": "password rotation"}
        ).json()
        return {p["chunk_id"] for p in body["passages"]}

    ids_first, ids_second = chunks_of(first), chunks_of(second)
    # Identical bytes, so the text matches equally well in both -- only the
    # scope separates them.
    assert ids_first and ids_second
    assert ids_first.isdisjoint(ids_second)


def test_an_analysis_never_reaches_the_other_document(client, api, searches, small_pdf):
    upload(client, small_pdf)
    second = upload(client, small_pdf).json()["document_id"]
    api.outcomes.extend(full_analysis(quote="vault every"))

    analysis_id = client.post("/api/analyses", json={"document_id": second}).json()["analysis_id"]
    report = poll(client, analysis_id)["report"]

    assert report["document_id"] == second
    quotes = [q for r in report["results"] for q in r["relevant_quotes"]]
    assert quotes and all(q["chunk_id"] == second for q in quotes)


# --------------------------------------------------------------------------
# Cross-cutting: tracing, auth, the spec
# --------------------------------------------------------------------------


def test_an_incoming_trace_id_is_honoured_and_returned(client):
    response = client.get("/api/health", headers={"X-Trace-Id": "abc123"})
    assert response.headers["X-Trace-Id"] == "abc123"


def test_a_trace_id_is_minted_when_none_is_sent(client):
    trace_id = client.get("/api/health").headers["X-Trace-Id"]
    assert len(trace_id) == 32
    assert client.get("/api/health").headers["X-Trace-Id"] != trace_id


def test_the_request_trace_runs_through_the_whole_analysis(
    client, api, searches, tmp_path, small_pdf
):
    """The demo's claim, asserted: one id from the request header appears on
    every line of the job, including the tool-call spans five criteria deep."""
    from contract_analyzer.logger import configure_logging

    document_id = upload(client, small_pdf).json()["document_id"]
    api.outcomes.extend(full_analysis())

    log_file = tmp_path / "app.jsonl"
    configure_logging("INFO", log_file, console=False, force=True)
    try:
        analysis_id = client.post(
            "/api/analyses", json={"document_id": document_id}, headers={"X-Trace-Id": "d" * 32}
        ).json()["analysis_id"]
        poll(client, analysis_id)
    finally:
        configure_logging("INFO", None, console=False, force=True)

    lines = [json.loads(line) for line in log_file.read_text().splitlines()]
    spans = {line.get("span") for line in lines}
    assert {"api.analysis", "analysis.criterion", "agent.call", "agent.tool"} <= spans
    assert [line for line in lines if line.get("trace_id") != "d" * 32] == []


def test_a_key_protects_everything_except_health_and_criteria(settings):
    # Constructed, not `model_copy`d: `api_key` is a `SecretStr`, and
    # `model_copy` skips validation, so an update would store a bare `str`.
    protected = Settings(**{**settings.model_dump(), "api_key": "s3cret"})
    with TestClient(create_app(protected, embedder=FakeEmbedder(settings), client=None,
                          static_dir=NO_BUNDLE)) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/criteria").status_code == 200
        assert client.get("/api/health").json()["auth_required"] is True

        assert client.get("/api/documents").status_code == 401
        assert client.get("/api/documents").json()["error"]["code"] == "unauthorized"
        assert client.get("/api/documents", headers={"X-API-Key": "wrong"}).status_code == 401
        assert client.get("/api/documents", headers={"X-API-Key": "s3cret"}).status_code == 200


def test_every_metrics_operation_answers_over_an_empty_database(client):
    """All four, and none of them 503. `metrics_unavailable` now means one
    thing only -- the process could not build a store -- and "nothing has run
    yet" is a fact about the system, not a failure of the endpoint.

    A run id with no spans is an empty list rather than a 404: the run may well
    be in `/metrics/runs` beside it, from a boot before this table existed."""
    for path in ("/api/metrics/summary", "/api/metrics/timeseries", "/api/metrics/runs",
                 "/api/monitor/stages", "/api/monitor/host", "/api/monitor/upstream"):
        assert client.get(path).status_code == 200, path

    response = client.get("/api/metrics/runs/x/spans")
    assert response.status_code == 200
    assert response.json() == []


def test_monitor_windows_match_kpi_and_refuse_the_rest(client):
    for path in ("/api/monitor/stages", "/api/monitor/host", "/api/monitor/upstream"):
        for window in ("30m", "1h", "24h", "7d", "30d"):
            assert client.get(f"{path}?window={window}").status_code == 200, (path, window)
        assert client.get(f"{path}?window=14d").status_code == 422, path


# --------------------------------------------------------------------------
# The serving model: one origin, one prefix, the front end underneath
# --------------------------------------------------------------------------


def test_every_route_is_behind_the_api_prefix(client):
    """The prefix is what lets one process serve the API and the browser client
    from one origin, so nothing may be reachable without it.

    `client` has no bundle mounted, so the unprefixed paths are plain 404s.
    With one mounted they would return the app -- which is the same statement:
    nothing there is the API."""
    for path in ("/documents", "/criteria", "/analyses", "/metrics/summary"):
        assert client.get(path).status_code == 404, path


def test_health_keeps_its_root_alias_for_the_container(client):
    """`/health` is what the Docker healthcheck and every `curl` in the docs
    target. It answers at both spellings and says the same thing."""
    assert client.get("/health").json() == client.get("/api/health").json()


def test_the_root_health_alias_is_not_a_second_operation(client):
    """Two spellings, one documented operation: the OpenAPI document is the
    connector deliverable, and a generator binding both would produce two
    methods for one endpoint."""
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/health" in paths
    assert "/health" not in paths


def test_an_unknown_api_path_is_a_404_in_this_apis_envelope(client):
    """Not `index.html` with a 200. Without the catch-all a typo'd route falls
    through to the static mount, which is the most confusing answer a generated
    client can be handed."""
    response = client.get("/api/documnets")
    assert response.status_code == 404
    body = response.json()["error"]
    assert body["code"] == "unknown_route"
    assert body["hint"]

    # Every method, not only GET: a POST to a mistyped route is the same
    # mistake and deserves the same answer.
    assert client.post("/api/analyses/x/cancle").status_code == 404


def test_the_app_starts_without_a_built_front_end(client):
    """The bundle is a build artefact. A fresh clone, the suite and `make api`
    before `make ui-build` all run without one, and `StaticFiles` raises on a
    missing directory -- so the mount is conditional. `client` is built against
    a directory that does not exist, which is the proof."""
    assert client.get("/api/health").status_code == 200
    # Nothing is mounted at "/", so a client-side route is a plain 404 rather
    # than a crash.
    assert client.get("/documents/1/analysis").status_code == 404


def test_the_openapi_document_is_fit_to_be_the_connector_spec(client):
    spec = client.get("/openapi.json").json()

    assert spec["info"]["title"] == "Contract Analyzer"
    assert "APIKeyHeader" in spec["components"]["securitySchemes"]

    operations = [
        (path, method, op)
        for path, methods in spec["paths"].items()
        for method, op in methods.items()
    ]
    assert len(operations) >= 17
    missing = [f"{m.upper()} {p}" for p, m, op in operations if not op.get("summary")]
    assert missing == []
    assert "Error" in spec["components"]["schemas"]
