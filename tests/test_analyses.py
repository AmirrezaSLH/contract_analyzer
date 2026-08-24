"""The analysis record: what survives the process that made it.

The failure this table fixes is invisible until a restart -- the API answered
every poll correctly, and then the report was gone -- which is exactly the kind
of bug that comes back. So the claims are pinned here rather than left to a
manual check:

* `mark_running` **upserts**, so the API's queued row is transitioned and the
  CLI's row is created, and neither surface overwrites the other's;
* `reconcile` turns `queued` and `running` into `interrupted` and **leaves
  every finished status alone**;
* **deleting a document keeps its analyses and their reports** -- the one that
  would be silently wrong if anybody added a foreign key.

No model and no embedder: these are about the row. What the *runs* write into
it arrives with `analyze_document`, in the next commit.
"""

from __future__ import annotations

import json
import struct

import pytest

from contract_analyzer.analyses import (
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
from contract_analyzer.report import AnalysisReport

DIM = 4
CLAUSE = "Supplier shall rotate credentials and encrypt data in transit at all times."
CRITERIA = get_criteria()


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
