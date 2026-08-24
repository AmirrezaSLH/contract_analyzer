"""One origin: the API serves the front end, and the two never collide.

Every test here supplies its own bundle in a `tmp_path`, because the real one
is a build artefact. A suite that reads
`src/contract_analyzer/api/static/` passes or fails on whether someone has run
`make ui-build` in this checkout, which is not a property of the code.

What is actually being pinned is the reason there is no CORS configuration
anywhere in this project: the browser only ever talks to one origin, so the
question of a second one never arises.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from contract_analyzer.api.main import create_app
from contract_analyzer.config import Settings
from contract_analyzer.embeddings.fake import FakeEmbedder

INDEX = "<!doctype html><title>Contract Analyzer</title><div id=root></div>"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        anthropic_api_key=None,
        embedding_provider="fake",
        embedding_model=None,
        embedding_dim=64,
        db_path=tmp_path / "contracts.db",
        raw_dir=tmp_path / "raw",
        assets_dir=tmp_path / "assets",
        log_file=None,
    )


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """What `vite build` leaves behind, in miniature."""
    root = tmp_path / "static"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX)
    (root / "assets" / "index-abc123.js").write_text("console.log(1)")
    return root


@pytest.fixture
def served(settings, bundle):
    app = create_app(settings, embedder=FakeEmbedder(settings), client=None, static_dir=bundle)
    with TestClient(app) as client:
        yield client


def test_the_root_is_the_front_end(served):
    response = served.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Contract Analyzer" in response.text


def test_a_hard_refresh_on_a_client_side_route_returns_the_app(served):
    """The one that `html=True` alone does not give.

    `html=True` serves index.html for a *directory*; `/documents/1/analysis` is
    neither a directory nor a file and never will be either -- it is a route the
    browser resolves. Without the fallback, a reload mid-analysis is a 404, and
    "do not refresh the page" is not an acceptable thing to say during a demo.
    """
    for path in ("/upload", "/library", "/documents/1/analysis", "/documents/12/chat"):
        response = served.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/html"), path
        assert "Contract Analyzer" in response.text


def test_a_real_asset_is_served_as_itself(served):
    response = served.get("/assets/index-abc123.js")
    assert response.status_code == 200
    assert "console.log" in response.text


def test_a_missing_asset_is_a_404_and_not_the_app(served):
    """A missing file under /assets/ means the bundle is broken. Answering it
    with index.html would turn that into a blank page and a MIME error in the
    console, instead of one clean 404 in the network panel."""
    response = served.get("/assets/index-gone.js")
    assert response.status_code == 404
    assert "Contract Analyzer" not in response.text


def test_the_api_still_wins_over_the_mount(served):
    """The mount is registered last, so it only sees what no route claimed."""
    assert served.get("/api/health").json()["status"] in ("ok", "degraded")
    assert served.get("/api/documents").json() == []
    assert served.get("/health").status_code == 200
    assert served.get("/docs").status_code == 200
    assert served.get("/openapi.json").status_code == 200


def test_an_unknown_api_path_is_json_even_with_a_bundle_mounted(served):
    """The failure this guards against only exists once something is mounted at
    `/`: without the catch-all, `/api/documnets` falls through and comes back as
    index.html with a 200, which a generated client reads as success."""
    response = served.get("/api/documnets")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "unknown_route"


def test_no_cors_middleware_is_configured(settings, bundle):
    """Not an oversight. The browser sees one origin -- Vite proxies /api in
    development and this process answers both in production -- so configuring
    CORS would be a symptom of having got the serving story wrong."""
    app = create_app(settings, embedder=None, client=None, static_dir=bundle)
    assert settings.api_cors_origins == []
    assert not any("CORS" in str(m.cls) for m in app.user_middleware)
