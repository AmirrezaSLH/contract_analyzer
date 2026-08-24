"""The MCP surface, offline: no API, no network, no key.

The suite is built around the claims that would be expensive to discover in a
demo, and each one is falsifiable here:

* **The tool list is the design.** Seven tools, no `ask_contract`, no wrapper
  around `POST /chat`. A host given two ways to ask a question picks one at
  random, and a host given a chat tool stops using the analysis pipeline.
* **A failure is something a model can act on.** Every error arrives as
  `code: message hint`, never a traceback and never a bare status.
* **`path` is refused over HTTP.** Over stdio the server reads a file the user
  could have opened themselves; over HTTP it would read its own filesystem on
  behalf of whoever can reach the port.
* **The connector adds nothing to the contract's text.** Passages are the
  API's, quotes are the analyzer's, and the only prose this package writes
  tells the host what to do next.
* **Every call is traceable and scoped.** One `X-Trace-Id` per tool call, the
  key when there is one, `X-Surface: mcp` on a run so the KPI page can slice
  on it.

`MockTransport` stands in for the API. It is not a stub of a stub: the bodies
below are the shapes `contract_analyzer/api/schemas.py` produces, and
`test_against_the_api.py` drives the same tools through the real application to
catch the day one of them stops being true.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_connector.client import ApiClient
from mcp_connector.config import ConnectorSettings
from mcp_connector.server import INSTRUCTIONS, build_server

PDF = b"%PDF-1.4\nnot really a contract\n"

TOOLS = (
    "get_started",
    "list_criteria",
    "upload_contract",
    "list_contracts",
    "analyze_compliance",
    "get_analysis",
    "search_contract",
)


# --------------------------------------------------------------------------
# A Contract Analyzer that is entirely made of dictionaries
# --------------------------------------------------------------------------


HEALTH = {
    "status": "ok", "version": "0.1.0", "db": True, "embedder": "fake",
    "embedding_model": "fake-hash", "answer_model": "claude-sonnet-5",
    "analysis_model": "claude-opus-5", "chat_models": ["claude-opus-5"],
    "retrieval_mode": "hybrid", "retrieval_top_k": 6, "max_upload_mb": 25.0,
    "api_workers": 2, "analysis_workers": 5, "key_present": True,
    "auth_required": False, "documents": 2, "analyses_running": 0,
}

CRITERIA = [
    {
        "id": "password_management",
        "requirement": "Password management controls.",
        "question": "Does the contract require password management?",
        "sub_requirements": [{"id": "PASS-01", "requirement": "Rotation."}],
        "states": ["Fully Compliant", "Partially Compliant", "Non-Compliant"],
    }
]

DOCUMENTS = [
    {
        "document_id": 2, "filename": "Second.pdf", "pages": 3, "chunks": 9,
        "spine_source": "outline", "ingested_at": "2026-08-24T10:00:00Z",
        "last_analysis": None,
    },
    {
        "document_id": 1, "filename": "Sample Contract.pdf", "pages": 21, "chunks": 84,
        "spine_source": "outline", "ingested_at": "2026-08-24T09:00:00Z",
        "last_analysis": {
            "analysis_id": "a1", "status": "done", "completed_at": "2026-08-24T09:05:00Z",
            "states": {"Fully Compliant": 4, "Partially Compliant": 1}, "needs_review": 1,
        },
    },
]

UPLOADED = {
    "document_id": 3, "filename": "Sample Contract.pdf", "pages": 21, "chunks": 84,
    "spine_source": "outline", "ingested_at": "2026-08-24T11:00:00Z", "elapsed_s": 4.2,
    "last_analysis": None,
}

QUEUED = {
    "analysis_id": "an-1", "document_id": 1, "status": "queued", "stage": "queued",
    "progress": {"done": 0, "total": 1},
    "criteria": [{"id": "password_management", "status": "queued"}],
    "totals": None, "trace_id": "t1", "created_at": "2026-08-24T12:00:00Z",
}

RUNNING = {**QUEUED, "status": "running", "stage": "criterion 1/1",
           "criteria": [{"id": "password_management", "status": "running"}]}

RESULT = {
    "criterion_id": "password_management",
    "compliance_requirement": "Password management controls.",
    "compliance_question": "Does the contract require password management?",
    "compliance_state": "Fully Compliant",
    "sub_requirements": [
        {"id": "PASS-01", "requirement": "Rotation.", "status": "met", "quote_indexes": [0]}
    ],
    "relevant_quotes": [{
        "text": "Supplier shall rotate credentials every ninety (90) days.",
        "evidence_id": "E1", "section_ref": "6.6 Password Management Standard",
        "page_display": "9", "chunk_id": 41, "verified": True,
    }],
    "rationale": "The clause is explicit about the rotation period.",
    "raw_confidence": 0.9, "confidence": 0.88, "confidence_components": {},
    "needs_review": False, "unresolved_errors": [], "structure_rounds": 0,
    "ended_by": "model", "tool_calls": 2, "usage": {}, "cost_usd": 0.14,
    "model": "claude-opus-5", "latency_s": 28.6,
}

DONE = {
    **QUEUED, "status": "done", "stage": "done", "progress": {"done": 1, "total": 1},
    "criteria": [{
        "id": "password_management", "status": "done", "state": "Fully Compliant",
        "confidence": 0.88, "needs_review": False, "latency_s": 28.6,
    }],
    "totals": {
        "criteria": 1, "latency_s": 28.6, "cost_usd": 0.14, "input_tokens": 0,
        "output_tokens": 0, "tool_calls": 2, "needs_review": 0, "capped": 0,
        "mean_confidence": 0.88,
    },
    "completed_at": "2026-08-24T12:01:00Z",
    "report": {
        "analysis_id": "an-1", "document_id": 1, "filename": "Sample Contract.pdf",
        "status": "done", "trace_id": "t1", "results": [RESULT],
        "totals": {
            "criteria": 1, "latency_s": 28.6, "cost_usd": 0.14, "input_tokens": 0,
            "output_tokens": 0, "tool_calls": 2, "needs_review": 0, "capped": 0,
            "mean_confidence": 0.88,
        },
        "cross_criterion_notes": [], "skipped": [], "error": None,
        "created_at": "2026-08-24T12:00:00Z", "completed_at": "2026-08-24T12:01:00Z",
    },
}

PASSAGES = {
    "document_id": 1, "query": "password rotation", "mode": "hybrid",
    "passages": [{
        "chunk_id": 41, "section": "6.6 Password Management Standard",
        "breadcrumb": "6. Identity and Access Management > 6.6 Password Management Standard",
        "page_display": "9", "element_type": "paragraph",
        "text": "Supplier shall rotate credentials every ninety (90) days.",
        "score": 0.031, "similarity": 0.82,
    }],
}


class FakeApi:
    """Every route the connector calls, answered from the dictionaries above.

    `requests` keeps each one, because half of what this connector does is
    decided in the request rather than the response: the surface header, the
    trace id, the key, the top_k the host never chose.
    """

    def __init__(self, **routes: object) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], object] = {
            ("GET", "/api/health"): HEALTH,
            ("GET", "/api/criteria"): CRITERIA,
            ("GET", "/api/documents"): DOCUMENTS,
            ("POST", "/api/documents"): (201, UPLOADED),
            ("POST", "/api/analyses"): (202, QUEUED),
            ("GET", "/api/analyses/an-1"): DONE,
            ("POST", "/api/documents/1/search"): PASSAGES,
            ("GET", "https://example.test/contract.pdf"): (200, PDF),
        }
        self.routes.update({_key(k): v for k, v in routes.items()})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for key in ((request.method, request.url.path), (request.method, str(request.url))):
            if key in self.routes:
                return _response(self.routes[key])
        return _response((404, {"error": {
            "code": "unknown_route",
            "message": f"No API route at {request.url.path}.",
            "hint": "Read /openapi.json for the routes this service publishes.",
        }}))

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def body_of(self, request: httpx.Request) -> dict:
        return json.loads(request.content or b"{}")


def _key(raw: str) -> tuple[str, str]:
    method, _, path = raw.partition(" ")
    return method, path


def _response(canned: object) -> httpx.Response:
    if isinstance(canned, tuple):
        status, payload = canned
    else:
        status, payload = 200, canned
    if isinstance(payload, bytes):
        return httpx.Response(status, content=payload)
    if isinstance(payload, Exception):
        raise payload
    return httpx.Response(status, json=payload)


# --------------------------------------------------------------------------
# Driving the tools
# --------------------------------------------------------------------------


def server_over(api: FakeApi, **overrides) -> tuple[object, ConnectorSettings]:
    """A server whose every request is answered by `api`.

    The settings are stated rather than read: a suite whose behaviour depends
    on the `.env` of the checkout it runs in is a suite that passes here and
    fails in CI.
    """
    stated = {
        "ca_api_url": "http://analyzer.test",
        "api_key": None,
        "mcp_transport": "stdio",
        "mcp_poll_seconds": 10.0,
        "mcp_search_top_k": 6,
    }
    settings = ConnectorSettings(**{**stated, **overrides})
    client = ApiClient(settings, transport=httpx.MockTransport(api))
    return build_server(settings, client), settings


def call(server, name: str, arguments: dict | None = None):
    """One tool call, out and back. `asyncio.run` rather than an async test, so
    the suite needs no plugin and reads like the rest of the project's."""

    async def go():
        async with Client(server) as client:
            return await client.call_tool(name, arguments or {})

    return asyncio.run(go())


def tools_of(server) -> dict:
    async def go():
        async with Client(server) as client:
            return {tool.name: tool for tool in await client.list_tools()}

    return asyncio.run(go())


# --------------------------------------------------------------------------
# The tool list is the design
# --------------------------------------------------------------------------


def test_the_tool_list_is_exactly_the_seven_the_design_settled_on():
    """No chat tool, and no second way to ask a question.

    `POST /chat` is the UI's endpoint: it would make the host pay a second
    model to answer what it was about to answer itself, replay a transcript it
    already owns, and give up streaming on the way. `search_contract` is the
    one retrieval tool for the same reason -- two of them and a host picks at
    random.
    """
    tools = tools_of(server_over(FakeApi())[0])
    assert set(tools) == set(TOOLS)
    assert not {"ask_contract", "chat", "delete_contract", "cancel_analysis"} & set(tools)


def test_every_tool_publishes_an_output_schema():
    """A host that knows the shape of a result asks for the right thing once,
    instead of calling twice to find out what came back."""
    for name, tool in tools_of(server_over(FakeApi())[0]).items():
        assert tool.outputSchema, name
        assert tool.description and len(tool.description) > 80, name


@pytest.mark.parametrize(
    ("name", "read_only", "open_world"),
    [
        ("get_started", True, False),
        ("list_criteria", True, False),
        ("list_contracts", True, False),
        ("get_analysis", True, False),
        ("search_contract", True, False),
        # Writes a document, and reaches an address the host named.
        ("upload_contract", False, True),
        # Spends money and starts a job; not read-only, not idempotent.
        ("analyze_compliance", False, False),
    ],
)
def test_the_annotations_say_which_tools_cost_something(name, read_only, open_world):
    tool = tools_of(server_over(FakeApi())[0])[name]
    assert tool.annotations.readOnlyHint is read_only
    assert tool.annotations.openWorldHint is open_world


def test_the_instructions_carry_the_protocol_not_a_readme():
    """Sent once at connect time, and the only thing a host reads before it
    decides what to call first."""
    assert "get_started" in INSTRUCTIONS
    assert "analyze_compliance" in INSTRUCTIONS
    assert "search_contract" in INSTRUCTIONS
    # The two rules the whole design rests on.
    assert "not one this system can stand behind" in INSTRUCTIONS
    assert "keeps no session" in INSTRUCTIONS


# --------------------------------------------------------------------------
# Orientation
# --------------------------------------------------------------------------


def test_get_started_reports_live_state_and_names_the_next_call():
    started = call(server_over(FakeApi())[0], "get_started").data
    assert started.key_present is True
    assert started.documents == 2
    assert started.criteria == 1
    assert started.api_url == "http://analyzer.test"
    assert "list_contracts" in started.next_step


def test_get_started_says_analysis_is_unavailable_before_one_is_started():
    """The failure this prevents: a minute of polling, then a 503 that was
    knowable at the first call."""
    api = FakeApi(**{"GET /api/health": {**HEALTH, "key_present": False}})
    started = call(server_over(api)[0], "get_started").data
    assert started.key_present is False
    assert "ANTHROPIC_API_KEY" in started.next_step
    assert "search_contract" in started.next_step  # what still works


def test_get_started_distinguishes_a_key_this_connector_lacks():
    """`auth_required` without `key_configured` is the one auth failure that is
    diagnosable before any other call is made."""
    api = FakeApi(**{"GET /api/health": {**HEALTH, "auth_required": True}})
    started = call(server_over(api)[0], "get_started").data
    assert started.auth_required is True and started.key_configured is False

    started = call(server_over(api, api_key="secret")[0], "get_started").data
    assert started.key_configured is True


def test_list_criteria_carries_the_sub_requirements():
    """The parts a verdict is built from. Without them a host can report that a
    contract is Partially Compliant but not why."""
    criteria = call(server_over(FakeApi())[0], "list_criteria").data
    assert [c.id for c in criteria] == ["password_management"]
    assert criteria[0].sub_requirements[0].id == "PASS-01"


# --------------------------------------------------------------------------
# Contracts
# --------------------------------------------------------------------------


def test_list_contracts_offers_the_analysis_a_contract_already_has():
    """A run costs a minute and real money, so a host should be able to offer
    the existing one instead."""
    contracts = call(server_over(FakeApi())[0], "list_contracts").data
    assert [c.document_id for c in contracts] == [2, 1]
    assert contracts[1].last_analysis.states == {
        "Fully Compliant": 4, "Partially Compliant": 1
    }
    assert contracts[0].last_analysis is None


def test_upload_by_path_reads_the_file_and_posts_it(tmp_path):
    path = tmp_path / "Sample Contract.pdf"
    path.write_bytes(PDF)
    api = FakeApi()
    uploaded = call(server_over(api)[0], "upload_contract", {"path": str(path)}).data

    assert uploaded.document_id == 3
    assert b"%PDF" in api.last.content
    assert api.last.headers["content-type"].startswith("multipart/form-data")
    # The one thing a host must not assume about ids.
    assert "different id" in uploaded.note


def test_upload_by_path_is_refused_over_http(tmp_path):
    """Over stdio the server reads a file the user could have opened anyway.
    Over HTTP it would read *its own* filesystem -- inside a container, on
    behalf of whoever can reach the port."""
    path = tmp_path / "c.pdf"
    path.write_bytes(PDF)
    server, _ = server_over(FakeApi(), mcp_transport="http")
    with pytest.raises(ToolError) as failure:
        call(server, "upload_contract", {"path": str(path)})
    assert "path_not_allowed" in str(failure.value)
    assert "url" in str(failure.value)


def test_upload_by_path_stays_under_the_configured_root(tmp_path):
    allowed = tmp_path / "contracts"
    allowed.mkdir()
    outside = tmp_path / "secrets.pdf"
    outside.write_bytes(PDF)
    server, _ = server_over(FakeApi(), mcp_upload_root=allowed)
    with pytest.raises(ToolError) as failure:
        call(server, "upload_contract", {"path": str(outside)})
    assert "path_not_allowed" in str(failure.value)


def test_upload_needs_exactly_one_of_path_and_url(tmp_path):
    server, _ = server_over(FakeApi())
    for arguments in ({}, {"path": str(tmp_path / "a.pdf"), "url": "https://x.test/a.pdf"}):
        with pytest.raises(ToolError) as failure:
            call(server, "upload_contract", arguments)
        assert "exactly one" in str(failure.value)


def test_upload_by_url_downloads_and_forwards_it():
    api = FakeApi()
    uploaded = call(
        server_over(api)[0], "upload_contract", {"url": "https://example.test/contract.pdf"}
    ).data
    assert uploaded.document_id == 3
    assert [str(r.url) for r in api.requests][0] == "https://example.test/contract.pdf"


def test_upload_by_url_refuses_something_that_is_not_a_pdf():
    """Refused here rather than after the bytes have been pulled through this
    process and posted to the API."""
    api = FakeApi(**{"GET https://example.test/contract.pdf": (200, b"<html>not a pdf")})
    with pytest.raises(ToolError) as failure:
        call(server_over(api)[0], "upload_contract",
             {"url": "https://example.test/contract.pdf"})
    assert "not_a_pdf" in str(failure.value)


def test_upload_by_url_refuses_a_scheme_the_connector_will_not_fetch():
    with pytest.raises(ToolError) as failure:
        call(server_over(FakeApi())[0], "upload_contract", {"url": "file:///etc/passwd"})
    assert "http(s)" in str(failure.value)


# --------------------------------------------------------------------------
# The five criteria
# --------------------------------------------------------------------------


def test_analyze_starts_a_job_and_says_how_to_wait_for_it():
    """`analyze_compliance` must never look like it answered: the run is
    60-180 s and a host that treats a `queued` as a verdict reports nothing."""
    api = FakeApi()
    started = call(server_over(api)[0], "analyze_compliance", {"document_id": 1}).data

    assert started.analysis_id == "an-1"
    assert started.status == "queued"
    assert started.poll_after_seconds == 10.0
    assert "get_analysis" in started.next_step


def test_analyze_declares_the_mcp_surface():
    """Without the header every HTTP submission is recorded as `api` and the
    KPI page cannot tell this connector's runs from the browser's."""
    api = FakeApi()
    call(server_over(api)[0], "analyze_compliance", {"document_id": 1})
    assert api.last.headers["X-Surface"] == "mcp"


def test_analyze_passes_a_subset_of_criteria_through():
    api = FakeApi()
    call(server_over(api)[0], "analyze_compliance",
         {"document_id": 1, "criterion_ids": ["password_management"]})
    assert api.body_of(api.last)["criteria"] == ["password_management"]


def test_an_unfinished_analysis_says_when_to_ask_again():
    api = FakeApi(**{"GET /api/analyses/an-1": RUNNING})
    state = call(server_over(api)[0], "get_analysis", {"analysis_id": "an-1"}).data
    assert state.status == "running"
    assert state.retry_after_seconds == 10.0
    assert "get_analysis again" in state.next_step
    assert state.verdicts == []


def test_a_finished_analysis_stops_asking_and_carries_the_verdicts():
    state = call(server_over(FakeApi())[0], "get_analysis", {"analysis_id": "an-1"}).data
    assert state.status == "done"
    assert state.retry_after_seconds is None
    assert state.states == {"Fully Compliant": 1}
    assert state.cost_usd == 0.14
    verdict = state.verdicts[0]
    assert verdict.state == "Fully Compliant"
    assert verdict.confidence == 0.88
    assert verdict.sub_requirements == {"PASS-01": "met"}


def test_summary_is_the_default_and_leaves_the_evidence_behind():
    """A full report is a great deal of text to carry in a conversation that
    then continues, and none of it changes what the host says next."""
    api = FakeApi()
    state = call(server_over(api)[0], "get_analysis", {"analysis_id": "an-1"}).data
    assert dict(api.last.url.params)["detail"] == "summary"
    assert state.verdicts[0].quotes == []
    assert state.verdicts[0].rationale == ""


def test_full_detail_carries_the_quotes_and_says_they_are_quotes():
    api = FakeApi()
    state = call(
        server_over(api)[0], "get_analysis", {"analysis_id": "an-1", "detail": "full"}
    ).data
    assert dict(api.last.url.params)["detail"] == "full"
    quote = state.verdicts[0].quotes[0]
    assert quote.text.startswith("Supplier shall rotate")
    assert quote.section_ref == "6.6 Password Management Standard"
    assert quote.verified is True
    assert "do not paraphrase" in state.next_step


def test_an_interrupted_run_is_not_reported_as_a_failure():
    """The model refusing and the machine going away want different copy: one
    is a result, the other is "run it again"."""
    api = FakeApi(**{"GET /api/analyses/an-1": {**QUEUED, "status": "interrupted"}})
    state = call(server_over(api)[0], "get_analysis", {"analysis_id": "an-1"}).data
    assert state.retry_after_seconds is None
    assert "went away" in state.next_step


# --------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------


def test_search_returns_the_contracts_own_words_with_where_to_find_them():
    passages = call(
        server_over(FakeApi())[0], "search_contract",
        {"document_id": 1, "query": "password rotation"},
    ).data
    assert passages.mode == "hybrid"
    found = passages.passages[0]
    assert found.text.startswith("Supplier shall rotate")
    assert found.section == "6.6 Password Management Standard"
    assert found.page_display == "9"
    assert "cite the section" in passages.note


def test_an_empty_search_tells_the_host_not_to_fall_back_on_general_knowledge():
    """The one failure this tool exists to prevent, and the one a user cannot
    detect: a model that finds nothing and answers from what contracts usually
    say."""
    api = FakeApi(**{"POST /api/documents/1/search": {**PASSAGES, "passages": []}})
    passages = call(
        server_over(api)[0], "search_contract", {"document_id": 1, "query": "arbitration"}
    ).data
    assert passages.passages == []
    assert "does not appear to cover it" in passages.note
    assert "general knowledge" in passages.note


def test_top_k_is_the_connectors_cap_and_not_the_hosts_choice():
    """A model that can ask for fifty passages will, and the cost lands in its
    own context window."""
    api = FakeApi()
    server, settings = server_over(api, mcp_search_top_k=3)
    assert "top_k" not in tools_of(server)["search_contract"].inputSchema["properties"]
    call(server, "search_contract", {"document_id": 1, "query": "x"})
    assert api.body_of(api.last)["top_k"] == 3


def test_search_is_scoped_to_the_document_in_the_url():
    """Isolation is the API's invariant, and this is the half of it the
    connector is responsible for: the id the host gave, in the path, never a
    corpus-wide search."""
    api = FakeApi()
    with pytest.raises(ToolError):
        call(server_over(api)[0], "search_contract", {"document_id": 7, "query": "x"})
    assert api.last.url.path == "/api/documents/7/search"


# --------------------------------------------------------------------------
# Failures, as something a model can act on
# --------------------------------------------------------------------------


def test_an_api_error_reaches_the_host_as_code_message_and_hint():
    """`document_not_found` plus "upload one" is what lets a host recover. A
    404, or a traceback, is not."""
    api = FakeApi(**{"GET /api/analyses/an-1": (404, {"error": {
        "code": "analysis_not_found",
        "message": "No analysis with id an-1.",
        "hint": "Open the document this belongs to and read its analyses, or run a new one.",
    }})})
    with pytest.raises(ToolError) as failure:
        call(server_over(api)[0], "get_analysis", {"analysis_id": "an-1"})
    message = str(failure.value)
    assert "analysis_not_found" in message
    assert "No analysis with id an-1." in message
    assert "run a new one" in message


def test_an_unreachable_api_names_the_url_and_what_to_do_about_it():
    """The most likely failure in a demo, and the one a traceback explains
    worst: nobody started the API."""
    api = FakeApi(**{"GET /api/health": httpx.ConnectError("connection refused")})
    with pytest.raises(ToolError) as failure:
        call(server_over(api)[0], "get_started")
    message = str(failure.value)
    assert "api_unreachable" in message
    assert "http://analyzer.test" in message
    assert "start.bash" in message or "docker-up" in message


def test_a_body_that_is_not_this_apis_envelope_still_becomes_a_usable_error():
    """A proxy in front of the API answering HTML is a real failure mode, and
    "Expecting value: line 1 column 1" helps nobody."""
    api = FakeApi(**{"GET /api/documents": (502, b"<html>Bad Gateway</html>")})
    with pytest.raises(ToolError) as failure:
        call(server_over(api)[0], "list_contracts")
    assert "http_502" in str(failure.value)
    assert "CA_API_URL" in str(failure.value)


def test_no_traceback_ever_reaches_the_host():
    api = FakeApi(**{"GET /api/documents": (500, {"error": {
        "code": "internal",
        "message": "The server failed to handle this request.",
        "hint": "The response's X-Trace-Id appears on every log line of this request.",
    }})})
    with pytest.raises(ToolError) as failure:
        call(server_over(api)[0], "list_contracts")
    assert "Traceback" not in str(failure.value)
    assert "internal" in str(failure.value)


# --------------------------------------------------------------------------
# What every request carries
# --------------------------------------------------------------------------


def test_every_call_carries_its_own_trace_id():
    """One id per tool call, not per session: a session is the host's
    conversation and can run for an hour."""
    api = FakeApi()
    server, _ = server_over(api)
    call(server, "list_contracts")
    call(server, "list_contracts")
    ids = [r.headers["X-Trace-Id"] for r in api.requests]
    assert all(len(i) == 32 for i in ids)
    assert len(set(ids)) == len(ids)


def test_the_api_key_is_sent_only_when_there_is_one():
    api = FakeApi()
    call(server_over(api)[0], "list_contracts")
    assert "x-api-key" not in api.last.headers

    api = FakeApi()
    call(server_over(api, api_key="secret")[0], "list_contracts")
    assert api.last.headers["X-API-Key"] == "secret"


def test_the_connector_never_reaches_past_the_api():
    """The boundary the whole package rests on: HTTP to one base URL, and no
    import of the analyzer that could turn into a database connection."""
    import mcp_connector.client as client_module
    import mcp_connector.schemas as schemas_module
    import mcp_connector.server as server_module

    for module in (client_module, schemas_module, server_module):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "import contract_analyzer" not in source
        assert "from contract_analyzer" not in source


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_a_blank_env_line_means_the_default_not_an_empty_value(monkeypatch):
    """`.env.example` ships these keys empty, because a key a reader can see is
    a key a reader can fill in. `CA_API_URL=` must not become a base URL of
    "", which fails at the first request with nothing useful to say."""
    for name in ("CA_API_URL", "MCP_UPLOAD_ROOT", "API_KEY"):
        monkeypatch.setenv(name, "")
    settings = ConnectorSettings()
    assert settings.api_url == f"http://127.0.0.1:{settings.backend_port}"
    assert settings.mcp_upload_root is None
    assert settings.api_key_value is None


def test_the_api_url_is_the_root_and_the_prefix_is_the_clients():
    """`CA_API_URL` is `http://api:8100`, never `.../api`: a caller that has to
    know where the prefix goes is a caller that will put it in twice."""
    settings = ConnectorSettings(ca_api_url="http://api:8100/")
    assert settings.api_url == "http://api:8100"
    assert str(ApiClient(settings).client.base_url).rstrip("/") == "http://api:8100/api"


def test_the_port_is_read_from_the_environment_like_every_other_one():
    """The project moved off hardcoded ports on purpose -- BACKEND_PORT,
    FRONTEND_PORT, MCP_PORT all live in .env together."""
    assert ConnectorSettings(mcp_port=9999).mcp_port == 9999
    assert ConnectorSettings(backend_port=9000).api_url == "http://127.0.0.1:9000"
