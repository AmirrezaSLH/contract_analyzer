"""The analysis record: what survives the process that made it.

The failure this table fixes is invisible until a restart -- the API answered
every poll correctly, and then the report was gone -- which is exactly the kind
of bug that comes back. So the claims are pinned here rather than left to a
manual check:

* a report written by `analyze_document` **round-trips out of `report_json`**
  as an equal `AnalysisReport`, which is the "no second schema" claim again;
* `reconcile` turns `queued` and `running` into `interrupted` and **leaves
  every finished status alone**;
* **deleting a document keeps its analyses and their reports** -- the one that
  would be silently wrong if anybody added a foreign key;
* a cancelled run keeps its partial report and its skipped ids, and a failed
  one keeps its error and has no report;
* `make analyze` writes a row with `surface='cli'`, and that row is readable
  through `GET /analyses/{id}` in an app built over the same database. That is
  the API-adds-no-logic invariant demonstrated rather than asserted.

Offline and keyless throughout: the model is the scripted SSE transport and the
embedder is never called, because retrieval is stubbed.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import ScriptedAPI, make_chunk, scripted_client
from contract_analyzer.analyses import (
    fail_analysis,
    finish_analysis,
    get_analysis,
    list_analyses,
    live_analyses,
    mark_running,
    queue_analysis,
    reconcile,
)
from contract_analyzer.compliance import get_criteria
from contract_analyzer.config import Settings
from contract_analyzer.db import get_db
from contract_analyzer.documents import delete_document
from contract_analyzer.embeddings.fake import FakeEmbedder
from contract_analyzer.generation import tools as T
from contract_analyzer.report import AnalysisReport, analyze_document
from test_report import script

DIM = 4
CLAUSE = "Supplier shall rotate credentials and encrypt data in transit at all times."
CRITERIA = get_criteria()

#: See `test_api.NO_BUNDLE`: an app under test serves the API, never a bundle
#: that may or may not have been built in this checkout.
NO_BUNDLE = Path(__file__).resolve().parent / "no-such-bundle"



@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        anthropic_api_key="k",
        embedding_provider="fake",
        embedding_model=None,
        embedding_dim=DIM,
        db_path=tmp_path / "contracts.db",
        raw_dir=tmp_path,
        assets_dir=tmp_path / "assets",
        log_file=None,
        analysis_workers=1,
        analysis_max_tool_calls=4,
        structure_fix_rounds=0,
    )


@pytest.fixture
def conn(settings):
    """One document and one chunk, the same shape `test_report.py` builds."""
    conn = get_db(settings)
    conn.execute(
        "INSERT INTO documents (id, path, filename, content_hash, page_count, spine_source) "
        "VALUES (1, 'data/raw/contract.pdf', 'contract.pdf', 'h', 21, 'headings')"
    )
    cursor = conn.execute(
        "INSERT INTO chunks (document_id, ordinal, content, page, page_label, section, "
        "section_path, embedding_model) VALUES (1, 0, ?, 8, '9', '6.6', ?, 'fake-hash')",
        (CLAUSE, json.dumps(["6. Identity", "6.6 Password Management Standard"])),
    )
    conn.execute(
        "INSERT INTO chunks_vec (chunk_id, document_id, embedding) VALUES (?, 1, ?)",
        (cursor.lastrowid, struct.pack(f"{DIM}f", *([0.5] * DIM))),
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def searches(monkeypatch):
    """Every `search_contract` returns the one clause, whoever asks."""
    from contract_analyzer.retrieval.base import RetrievalResult

    def retrieve(question, conn, embedder=None, settings=None, *, document_id, mode=None,
                 top_k=None, candidates=None):
        return RetrievalResult(question=question, mode=mode or "hybrid", document_id=document_id,
                               chunks=[make_chunk(1, CLAUSE)], candidates=20, top_k=top_k or 6)

    monkeypatch.setattr(T, "retrieve", retrieve)


def run(settings, conn, api=None, **kw):
    """One analysis of document 1, through the scripted model."""
    return analyze_document(1, conn, object(), settings, scripted_client(api or script()), **kw)


def stored(conn, analysis_id="a1", *, document_id=1, filename="contract.pdf", status="done"):
    """A finished analysis without going near a model: enough for the tests
    that are about the row rather than about the run."""
    queue_analysis(conn, analysis_id, document_id, filename=filename, criteria=["c1"])
    report = AnalysisReport(
        analysis_id=analysis_id, document_id=document_id, filename=filename, status=status,
        trace_id="t" * 32, created_at="2026-08-24T00:00:00+00:00",
        completed_at="2026-08-24T00:01:00+00:00",
    )
    finish_analysis(conn, analysis_id, report)
    return report


# --------------------------------------------------------------------------
# What a run leaves behind
# --------------------------------------------------------------------------


def test_a_run_writes_one_row_and_the_report_round_trips_out_of_it(settings, conn, searches):
    """The no-second-schema claim: the bytes in `report_json` are the report."""
    report = run(settings, conn)

    record = get_analysis(conn, report.analysis_id)
    assert record is not None
    assert record.status == "done" and record.surface == "cli"
    assert record.document_id == 1 and record.filename == "contract.pdf"
    assert record.trace_id == report.trace_id
    assert record.created_at and record.started_at and record.completed_at
    assert record.report() == report


def test_the_derived_columns_are_filled_from_the_report(settings, conn, searches):
    """They are field reads off a report this function is already holding, so
    the metrics store inherits a populated table instead of a backfill."""
    report = run(settings, conn)
    record = get_analysis(conn, report.analysis_id)

    assert record.criteria_requested == len(CRITERIA)
    assert record.criteria_completed == len(report.results)
    assert record.criteria_skipped == 0
    assert record.cost_usd == report.totals.cost_usd
    assert record.input_tokens == report.totals.input_tokens
    assert record.output_tokens == report.totals.output_tokens
    assert record.tool_calls == report.totals.tool_calls
    assert record.mean_confidence == report.totals.mean_confidence
    assert record.needs_review == report.totals.needs_review

    quotes = [q for r in report.results for q in r.relevant_quotes]
    assert record.quotes_total == len(quotes) > 0
    assert record.quotes_verified == sum(1 for q in quotes if q.verified)

    # The Evaluator has landed, so the columns reserved for it are no longer
    # empty. They stay *nullable* rather than defaulting to 0, because a row
    # written before any of this existed must keep saying "nobody was asked"
    # rather than "nothing was accepted" -- those are different facts and the
    # KPI page has to be able to tell them apart.
    assert record.evaluator_accepted == report.totals.accepted == len(report.results)
    assert record.evaluator_revised == report.totals.revised == 0
    assert record.evaluator_fallback == report.totals.fallback == 0
    assert record.evaluator_unevaluated == report.totals.unevaluated == 0
    assert record.evaluator_cost_usd == report.totals.evaluator_cost_usd > 0


def test_a_cancelled_run_persists_its_partial_report_and_its_skipped_ids(settings, conn, searches):
    report = run(settings, conn, cancelled=lambda: True)
    record = get_analysis(conn, report.analysis_id)

    assert record.status == "cancelled"
    assert record.criteria_skipped == len(CRITERIA) and record.criteria_completed == 0
    assert record.report().skipped == [c.id for c in CRITERIA]


def test_a_failed_run_persists_the_error_and_no_report(settings, conn, searches):
    api = ScriptedAPI(500)
    with pytest.raises(Exception):  # noqa: B017 - the SDK's own status error
        run(settings, conn, api, analysis_id="fixed-id")

    record = get_analysis(conn, "fixed-id")
    assert record.status == "failed"
    assert record.error and record.report_json is None
    assert record.report() is None
    assert record.completed_at


def test_queueing_then_running_keeps_the_surface_that_queued_it(conn):
    """`mark_running` is an upsert so the CLI, which never queues, still gets a
    row -- and so the API's row is transitioned rather than duplicated."""
    queue_analysis(conn, "a1", 1, filename="contract.pdf", criteria=["a", "b"], surface="api")
    assert get_analysis(conn, "a1").status == "queued"

    mark_running(conn, "a1", document_id=1, criteria=["a", "b"], surface="cli")
    record = get_analysis(conn, "a1")
    assert record.status == "running" and record.started_at
    assert record.surface == "api"  # the worker did not rewrite who asked
    assert record.filename == "contract.pdf"

    mark_running(conn, "b2", document_id=1, filename="contract.pdf", surface="cli")
    assert get_analysis(conn, "b2").surface == "cli"
    assert len(list_analyses(conn, 1)) == 2


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


def test_reconcile_interrupts_the_unfinished_and_leaves_the_rest(conn):
    for analysis_id, status in (
        ("q", "queued"), ("r", "running"), ("d", "done"), ("f", "failed"), ("c", "cancelled")
    ):
        queue_analysis(conn, analysis_id, 1)
        conn.execute("UPDATE analyses SET status = ? WHERE analysis_id = ?",
                     (status, analysis_id))
    conn.commit()

    assert reconcile(conn) == 2
    assert get_analysis(conn, "q").status == "interrupted"
    assert get_analysis(conn, "r").status == "interrupted"
    assert get_analysis(conn, "r").completed_at
    assert [get_analysis(conn, i).status for i in ("d", "f", "c")] == \
        ["done", "failed", "cancelled"]

    # Idempotent: a second startup has nothing left to close.
    assert reconcile(conn) == 0


def test_a_finished_row_is_not_reopened_by_a_late_failure(conn, settings, searches):
    """`fail_analysis` only touches a run that has not ended, so the API's
    belt-and-braces call cannot overwrite a report that already landed."""
    report = run(settings, conn)
    assert fail_analysis(conn, report.analysis_id, "too late") is False
    assert get_analysis(conn, report.analysis_id).status == "done"


def test_live_analyses_is_what_a_delete_checks(conn):
    queue_analysis(conn, "live", 1)
    queue_analysis(conn, "over", 1)
    conn.execute("UPDATE analyses SET status = 'done' WHERE analysis_id = 'over'")
    conn.commit()

    assert [r.analysis_id for r in live_analyses(conn, 1)] == ["live"]
    assert live_analyses(conn, 2) == []


# --------------------------------------------------------------------------
# The report outlives the contract
# --------------------------------------------------------------------------


def test_deleting_a_document_keeps_its_analyses_and_their_reports(settings, conn):
    """No foreign key, on purpose. The report is the deliverable and it is
    self-contained; a record that vanishes because someone tidied up the corpus
    is not a record. This is the assertion a later `REFERENCES` would break."""
    report = stored(conn)

    assert delete_document(conn, 1, settings) is True
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0

    record = get_analysis(conn, report.analysis_id)
    assert record is not None
    assert record.filename == "contract.pdf"  # denormalised, so it still reads
    assert record.report() == report
    assert [r.analysis_id for r in list_analyses(conn, 1)] == [report.analysis_id]


# --------------------------------------------------------------------------
# The invariant: the API adds no logic the command line does not have
# --------------------------------------------------------------------------


def test_a_cli_run_is_readable_through_the_api(settings, conn, searches):
    """`make analyze` with the API stopped, then the API started over the same
    database. Demonstrated rather than asserted: the row the CLI wrote is the
    row the HTTP layer reads."""
    from contract_analyzer.api.main import create_app

    report = run(settings, conn)
    assert get_analysis(conn, report.analysis_id).surface == "cli"

    app = create_app(settings, embedder=FakeEmbedder(settings), client=object(),
                     static_dir=NO_BUNDLE)
    with TestClient(app) as client:
        body = client.get(f"/api/analyses/{report.analysis_id}").json()
        assert body["status"] == "done"
        assert AnalysisReport.model_validate(body["report"]) == report
        assert [c["id"] for c in body["criteria"]] == [r.criterion_id for r in report.results]
        assert body["progress"] == {"done": len(CRITERIA), "total": len(CRITERIA)}

        listed = client.get("/api/analyses", params={"document_id": 1}).json()
        assert [a["analysis_id"] for a in listed] == [report.analysis_id]
        assert client.get("/api/documents/1").json()["analyses"][0]["analysis_id"] \
            == report.analysis_id


def test_streaming_and_cancelling_a_run_this_process_does_not_own_is_a_409(
    settings, conn, searches
):
    """Durable is not distributed: the cancel flag and the event stream are
    per-process objects. Saying so with a 409 beats a 404 that contradicts the
    GET the client just made."""
    from contract_analyzer.api.main import create_app

    queue_analysis(conn, "elsewhere", 1, filename="contract.pdf", criteria=["a"])

    app = create_app(settings, embedder=FakeEmbedder(settings), client=object(),
                     static_dir=NO_BUNDLE)
    with TestClient(app) as client:
        # The lifespan reconciled the row it found queued, so it reads honestly.
        assert client.get("/api/analyses/elsewhere").json()["status"] == "interrupted"

        for response in (
            client.post("/api/analyses/elsewhere/cancel"),
            client.get("/api/analyses/elsewhere/events"),
        ):
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "not_live_here"

        missing = client.post("/api/analyses/nosuchid/cancel")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "analysis_not_found"
