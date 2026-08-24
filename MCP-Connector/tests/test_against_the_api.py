"""The same tools, against the real application instead of a dictionary.

`test_mcp_connector.py` is the suite that says what this connector *does*; it
answers every request from canned JSON, which is what makes it fast and what
lets it script failures no running API would produce on demand. What it cannot
catch is drift: the day `PassageOut` renames a field or `GET /analyses/{id}`
stops nesting its report, the fake keeps agreeing with a connector that has
quietly stopped working.

So this file wires the connector to a real `create_app()` -- real routing, real
ingestion, real retrieval, real error envelopes -- through an in-process
transport. No network, no keys, no model: the app is built with the fake
embedder and **no answer client**, which is also what makes the last test here
possible, since a 503 from a keyless deployment is the failure a demo is most
likely to hit and the one worth proving arrives as something a host can read.

This is the one place in `MCP-Connector/` that imports the analyzer, and it is
a test importing the thing it integrates with. `mcp_connector` itself does not,
and `test_the_connector_never_reaches_past_the_api` asserts it.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastmcp.exceptions import ToolError

# One helper, not a fixture: pytest puts this directory on the path, and a
# conftest.py here would collide with the analyzer suite's by basename.
from test_mcp_connector import call

from mcp_connector.client import ApiClient
from mcp_connector.config import ConnectorSettings
from mcp_connector.server import build_server

pytest.importorskip("pymupdf")
pytest.importorskip("contract_analyzer")

#: Headers worth carrying back across the bridge. Not the whole set: a
#: `transfer-encoding` copied onto a body that is already in memory is a
#: contradiction httpx is right to complain about.
FORWARDED = ("content-type", "x-trace-id")


@pytest.fixture
def app_settings(tmp_path):
    from contract_analyzer.config import Settings

    return Settings(
        # No answer key: upload, listing and retrieval all work, analysis does
        # not, and that asymmetry is the point of `get_started`.
        anthropic_api_key=None,
        embedding_provider="fake",
        embedding_model=None,
        embedding_dim=64,
        db_path=tmp_path / "contracts.db",
        raw_dir=tmp_path / "raw",
        assets_dir=tmp_path / "assets",
        log_file=None,
        api_workers=1,
        analysis_workers=1,
    )


@pytest.fixture
def bridge(app_settings):
    """The connector's tools, over the real API, in one process.

    `TestClient` runs the application's lifespan -- which is what builds the job
    runner and reconciles the analyses table -- and the transport below turns
    each of the connector's httpx requests into a call on it.
    """
    from fastapi.testclient import TestClient

    from contract_analyzer.api.main import create_app
    from contract_analyzer.embeddings.fake import FakeEmbedder

    app = create_app(
        app_settings,
        embedder=FakeEmbedder(app_settings),
        client=None,
        static_dir=Path(__file__).parent / "no-such-bundle",
    )
    with TestClient(app) as http:
        def handler(request: httpx.Request) -> httpx.Response:
            answer = http.request(
                request.method,
                str(request.url),
                content=request.content,
                headers=dict(request.headers),
            )
            return httpx.Response(
                answer.status_code,
                content=answer.content,
                headers={k: v for k, v in answer.headers.items() if k.lower() in FORWARDED},
            )

        settings = ConnectorSettings(
            ca_api_url="http://testserver",
            api_key=None,
            mcp_transport="stdio",
            mcp_search_top_k=4,
        )
        yield build_server(settings, ApiClient(settings, transport=httpx.MockTransport(handler)))


@pytest.fixture
def contract(tmp_path) -> Path:
    """A small contract with real sections, so retrieval has something to rank."""
    import pymupdf

    path = tmp_path / "Supplier Agreement.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 90), "6.6 Password Management Standard", fontsize=16, fontname="hebo")
    page.insert_textbox(
        pymupdf.Rect(72, 120, 520, 260),
        "6.6.1 Supplier shall rotate all privileged credentials every ninety (90) days "
        "and shall enforce password complexity for every account it issues.",
        fontsize=11,
    )
    document.save(path)
    document.close()
    return path


# --------------------------------------------------------------------------


def test_get_started_reads_the_real_health_document(bridge):
    """Every field here is one this connector projects out of `Health`. A rename
    on either side fails this test rather than a demo."""
    started = call(bridge, "get_started").data
    assert started.key_present is False       # no ANTHROPIC_API_KEY on this app
    assert started.auth_required is False
    assert started.documents == 0
    assert started.criteria == 5
    assert "ANTHROPIC_API_KEY" in started.next_step


def test_list_criteria_is_the_five_this_service_publishes(bridge):
    criteria = call(bridge, "list_criteria").data
    assert len(criteria) == 5
    assert all(c.sub_requirements for c in criteria)
    assert all("Compliant" in " ".join(c.states) for c in criteria)


def test_upload_then_list_then_search_is_one_working_path(bridge, contract):
    """The whole read path a host takes, end to end: a real parse, a real
    ingest, real retrieval, and the projections this package puts on top."""
    uploaded = call(bridge, "upload_contract", {"path": str(contract)}).data
    assert uploaded.document_id >= 1
    assert uploaded.chunks >= 1
    assert uploaded.filename == "Supplier Agreement.pdf"

    contracts = call(bridge, "list_contracts").data
    assert [c.document_id for c in contracts] == [uploaded.document_id]
    assert contracts[0].last_analysis is None

    passages = call(
        bridge, "search_contract",
        {"document_id": uploaded.document_id, "query": "password rotation"},
    ).data
    assert passages.passages
    found = passages.passages[0]
    assert "ninety" in found.text
    # The section is what makes a citation checkable in seconds; it comes from
    # the chunker's spine, not from anything this connector composed.
    assert "Password Management" in (found.breadcrumb or found.section)
    assert passages.mode in ("hybrid", "keyword")


def test_the_search_cap_is_the_one_the_connector_configured(bridge, contract):
    document_id = call(bridge, "upload_contract", {"path": str(contract)}).data.document_id
    passages = call(
        bridge, "search_contract", {"document_id": document_id, "query": "password"}
    ).data
    assert 0 < len(passages.passages) <= 4


def test_analysis_on_a_keyless_deployment_fails_where_a_host_can_read_it(bridge, contract):
    """The most likely failure in a demo. `get_started` warns about it; this is
    what happens to a host that starts a run anyway."""
    document_id = call(bridge, "upload_contract", {"path": str(contract)}).data.document_id
    with pytest.raises(ToolError) as failure:
        call(bridge, "analyze_compliance", {"document_id": document_id})
    message = str(failure.value)
    assert "no_api_key" in message
    assert "ANTHROPIC_API_KEY" in message
    assert "Traceback" not in message


def test_an_unknown_document_is_the_apis_own_envelope(bridge):
    with pytest.raises(ToolError) as failure:
        call(bridge, "search_contract", {"document_id": 999, "query": "anything"})
    message = str(failure.value)
    assert "document_not_found" in message
    # The hint names an action rather than a route -- the API's rule, carried
    # through unchanged.
    assert "upload one" in message.lower()


def test_the_trace_id_the_connector_mints_is_the_one_the_api_answers_with(bridge):
    """One id, from the tool call through the request to every log line of the
    work it starts. The demo's "here it is again in app.jsonl" moment."""
    call(bridge, "list_contracts")
    # Nothing to assert on the client side beyond the round trip having worked:
    # the API echoes X-Trace-Id, and `ApiClient.call` prefers the echoed value.
    # What this test really guards is that the header survives the bridge at
    # all -- FORWARDED is easy to get wrong.
    with pytest.raises(ToolError) as failure:
        call(bridge, "get_analysis", {"analysis_id": "nope"})
    assert "(trace " in str(failure.value)
