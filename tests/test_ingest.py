"""Ingestion pipeline: idempotency, the three indexes, and the guards.

Idempotency is this step's main claim, and it is a claim about *cost*: running
`make ingest` twice must not re-parse and must not re-embed. So the fake
embedder counts the texts it is handed and the second run has to leave that
counter alone -- asserting on row counts would pass even if the whole contract
were embedded again and written identically.

The other thing under test is that the three indexes stay in step. `chunks` is
the source of truth; `chunks_vec` and `chunks_fts` are maintained by a cascade
and by triggers, which is exactly the kind of machinery that works until a
document is replaced and then quietly leaves orphans behind.

Everything runs on a temp database with the `fake` embedder: no network, no
key, and -- apart from the two `needs_sample` tests -- no corpus files.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from conftest import SAMPLE_PDF, needs_sample
from contract_analyzer.config import Settings
from contract_analyzer.db import get_db
from contract_analyzer.embeddings.fake import FakeEmbedder
from contract_analyzer.ingest.pipeline import (
    ModelMismatch,
    collect_paths,
    ingest_file,
    ingest_paths,
)

pymupdf = pytest.importorskip("pymupdf")

DIM = 32

_CLAUSE = (
    "Vendor shall maintain a documented control covering {topic}, review it at "
    "least annually, and provide the Customer with evidence of the review on "
    "request within thirty days of that request being made in writing."
)

_TOPICS = [
    "privileged credential rotation",
    "multi-factor authentication for administrative access",
    "encryption of data in transit",
    "asset inventory and ownership",
    "background checks for personnel",
    "security awareness training",
]


def write_contract(path: Path, *, pages: int = 3, marker: str = "alpha") -> Path:
    """A small but genuinely parseable contract: numbered clauses, several pages."""
    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page()
        page.insert_text(
            (72, 90),
            f"{index + 6}. Section {index + 6} Security Obligations",
            fontsize=16,
            fontname="hebo",
        )
        top = 130.0
        for k in range(6):
            topic = _TOPICS[k % len(_TOPICS)]
            page.insert_textbox(
                pymupdf.Rect(72, top, 520, top + 70),
                f"{index + 6}.{k + 1} Clause {k + 1}. "
                + _CLAUSE.format(topic=topic)
                + f" This obligation is designated {marker}.",
                fontsize=11,
            )
            top += 80
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        embedding_provider="fake",
        embedding_model=None,
        embedding_dim=DIM,
        db_path=tmp_path / "contracts.db",
        raw_dir=tmp_path / "raw",
        assets_dir=tmp_path / "assets",
        log_file=None,
    )


@pytest.fixture
def conn(settings: Settings):
    connection = get_db(settings)
    yield connection
    connection.close()


@pytest.fixture
def embedder(settings: Settings) -> FakeEmbedder:
    return FakeEmbedder(settings)


@pytest.fixture
def raw(settings: Settings) -> Path:
    settings.raw_dir.mkdir(parents=True, exist_ok=True)
    write_contract(settings.raw_dir / "msa.pdf")
    return settings.raw_dir


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "documents": conn.execute("SELECT count(*) FROM documents").fetchone()[0],
        "chunks": conn.execute("SELECT count(*) FROM chunks").fetchone()[0],
        "vec": conn.execute("SELECT count(*) FROM chunks_vec").fetchone()[0],
        "fts": conn.execute("SELECT count(*) FROM chunks_fts").fetchone()[0],
    }


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_ingest_fills_all_four_tables(raw, conn, embedder, settings):
    [result] = ingest_paths([raw], conn, embedder, settings)

    assert result.status == "ingested"
    assert result.chunks > 0
    assert result.pages == 3

    totals = counts(conn)
    assert totals["documents"] == 1
    # chunks is the source of truth; the other two are indexes over it, and a
    # disagreement here means a citation that retrieval cannot reach.
    assert totals["chunks"] == totals["vec"] == totals["fts"] == result.chunks


def test_the_document_row_records_what_the_parser_found(raw, conn, embedder, settings):
    ingest_paths([raw], conn, embedder, settings)
    row = conn.execute("SELECT * FROM documents").fetchone()

    assert row["filename"] == "msa.pdf"
    assert row["page_count"] == 3
    assert len(row["content_hash"]) == 64
    assert not row["path"].endswith("/")


def test_the_document_row_records_where_the_sections_came_from(raw, conn, embedder, settings):
    """A citation naming a section should be traceable to who decided the section.

    Word writes no /Outlines, so this file's breadcrumbs were inferred from its
    own headings and clause numbering. Storing that is the difference between
    a reviewer trusting a section reference and having to check it.
    """
    [result] = ingest_paths([raw], conn, embedder, settings)
    row = conn.execute("SELECT spine_source, has_outline FROM documents").fetchone()

    assert row["spine_source"] == "headings"
    assert row["has_outline"] == 0
    assert result.spine_source == "headings"


def test_chunk_rows_round_trip_their_json_fields(raw, conn, embedder, settings):
    ingest_paths([raw], conn, embedder, settings)
    row = conn.execute("SELECT * FROM chunks ORDER BY ordinal").fetchone()

    assert row["embedding_model"] == f"fake-hash-{DIM}"
    assert isinstance(json.loads(row["section_path"]), list)
    assert row["bbox"] is None or len(json.loads(row["bbox"])) == 4
    assert row["token_count"] > 0
    assert row["element_type"] in {"paragraph", "table", "figure", "equation", "caption"}


def test_a_single_page_chunk_stores_null_rather_than_a_repeated_page(
    raw, conn, embedder, settings
):
    """So "p.4" and "p.4-5" stay distinguishable in SQL, not only in Python."""
    ingest_paths([raw], conn, embedder, settings)
    rows = conn.execute("SELECT page, page_end FROM chunks").fetchall()

    assert rows
    assert all(r["page_end"] is None or r["page_end"] > r["page"] for r in rows)


def test_stored_vectors_are_searchable(raw, conn, embedder, settings):
    """A vector that cannot be queried back is the same as no vector at all."""
    import sqlite_vec

    ingest_paths([raw], conn, embedder, settings)
    query = sqlite_vec.serialize_float32(embedder.embed_query("privileged credential rotation"))
    hits = conn.execute(
        "SELECT chunk_id, distance FROM chunks_vec WHERE embedding MATCH ? AND k = 3"
        " ORDER BY distance",
        (query,),
    ).fetchall()

    assert len(hits) == 3
    ids = {row[0] for row in conn.execute("SELECT id FROM chunks")}
    assert all(hit["chunk_id"] in ids for hit in hits)


def test_full_text_index_finds_the_body_text(raw, conn, embedder, settings):
    ingest_paths([raw], conn, embedder, settings)
    # Quoted, because FTS5 reads a bare hyphen as a column filter: the same
    # trap `GOV-01` and `TLS 1.2` will spring on the keyword retriever.
    hits = conn.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?", ('"multi-factor"',)
    ).fetchall()

    assert hits, "the FTS triggers did not index the inserted chunks"


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def test_re_ingesting_an_unchanged_file_costs_nothing(raw, conn, embedder, settings):
    first = ingest_paths([raw], conn, embedder, settings)[0]
    calls_after_first = embedder.calls
    assert calls_after_first == first.chunks

    second = ingest_paths([raw], conn, embedder, settings)[0]

    assert second.status == "skipped"
    assert second.chunks == first.chunks
    # The point of hashing before parsing: not one text was embedded again.
    assert embedder.calls == calls_after_first
    assert counts(conn)["chunks"] == first.chunks


def test_a_skipped_file_still_reports_its_spine_source(raw, conn, embedder, settings):
    """The CLI prints one line per file; a skipped one must not print 'none'."""
    ingest_paths([raw], conn, embedder, settings)
    second = ingest_paths([raw], conn, embedder, settings)[0]

    assert second.status == "skipped"
    assert second.spine_source == "headings"


def test_reingest_forces_a_rebuild_of_an_unchanged_file(raw, conn, embedder, settings):
    first = ingest_paths([raw], conn, embedder, settings)[0]
    second = ingest_paths([raw], conn, embedder, settings, force=True)[0]

    assert second.status == "replaced"
    assert second.chunks == first.chunks
    assert embedder.calls == first.chunks * 2
    totals = counts(conn)
    assert totals["documents"] == 1
    assert totals["chunks"] == totals["vec"] == totals["fts"] == second.chunks


def test_a_changed_file_is_replaced_and_leaves_no_orphans(raw, conn, embedder, settings):
    first = ingest_paths([raw], conn, embedder, settings)[0]
    # A longer contract, so the chunk count genuinely changes and an orphan
    # would show up as a count mismatch rather than coincidentally lining up.
    write_contract(raw / "msa.pdf", pages=5, marker="beta")

    second = ingest_paths([raw], conn, embedder, settings)[0]

    assert second.status == "replaced"
    assert second.chunks != first.chunks
    totals = counts(conn)
    assert totals["documents"] == 1
    assert totals["chunks"] == totals["vec"] == totals["fts"] == second.chunks
    # The old text is gone from the FTS index too, not merely from `chunks`.
    stale = conn.execute(
        "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'alpha'"
    ).fetchone()[0]
    assert stale == 0


def test_replacing_a_document_clears_its_assets(raw, conn, embedder, settings):
    ingest_paths([raw], conn, embedder, settings)
    orphan = settings.assets_dir / "msa" / "p001_stale.png"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"not really a png")

    write_contract(raw / "msa.pdf", pages=4, marker="gamma")
    ingest_paths([raw], conn, embedder, settings)

    # A figure that moved or vanished must not leave a file behind that a stale
    # asset_path could still cite.
    assert not orphan.exists()


# --------------------------------------------------------------------------
# The guards
# --------------------------------------------------------------------------


def test_a_different_embedding_model_refuses_the_whole_run(raw, conn, embedder, settings):
    ingest_paths([raw], conn, embedder, settings)
    other = FakeEmbedder(settings.model_copy(update={"embedding_model": "other-hash"}))
    assert other.name != embedder.name

    write_contract(raw / "second.pdf", marker="delta")
    with pytest.raises(ModelMismatch) as excinfo:
        ingest_paths([raw], conn, other, settings)

    # Both names in the message: the fix is to change one of them back.
    assert embedder.name in str(excinfo.value)
    assert other.name in str(excinfo.value)
    assert counts(conn)["documents"] == 1


def test_a_corrupt_file_fails_alone_and_the_run_continues(raw, conn, embedder, settings):
    (raw / "broken.pdf").write_bytes(b"%PDF-1.4 this is not a pdf")

    results = ingest_paths([raw], conn, embedder, settings)
    by_name = {r.path.name: r for r in results}

    assert by_name["broken.pdf"].status == "failed"
    assert by_name["broken.pdf"].error
    assert by_name["msa.pdf"].status == "ingested"
    assert counts(conn)["documents"] == 1


def test_a_missing_file_is_reported_not_raised(conn, embedder, settings, tmp_path):
    result = ingest_file(tmp_path / "nope.pdf", conn, embedder, settings)

    assert result.status == "failed"
    assert "FileNotFoundError" in (result.error or "")


def test_a_failed_file_leaves_no_half_written_document(raw, conn, embedder, settings):
    """The whole write is one transaction: a documents row without its chunks
    would be reported as ingested and answer nothing."""
    (raw / "broken.pdf").write_bytes(b"%PDF-1.4 this is not a pdf")
    ingest_paths([raw], conn, embedder, settings)

    orphans = conn.execute(
        "SELECT count(*) FROM documents d WHERE NOT EXISTS "
        "(SELECT 1 FROM chunks c WHERE c.document_id = d.id)"
    ).fetchone()[0]
    assert orphans == 0


# --------------------------------------------------------------------------
# Dry run
# --------------------------------------------------------------------------


def test_dry_run_writes_nothing_and_needs_no_embedder(raw, conn, embedder, settings):
    results = ingest_paths([raw], None, None, settings, dry_run=True)

    assert [r.status for r in results] == ["dry-run"]
    assert results[0].chunks > 0
    assert results[0].report is not None
    assert results[0].report.elements_in > 0
    assert counts(conn) == {"documents": 0, "chunks": 0, "vec": 0, "fts": 0}
    assert embedder.calls == 0


# --------------------------------------------------------------------------
# Path collection
# --------------------------------------------------------------------------


def test_collect_paths_walks_directories_and_deduplicates(raw, settings):
    write_contract(raw / "second.pdf")
    (raw / "notes.txt").write_text("not a document the parser knows")

    found = collect_paths([raw, raw / "msa.pdf"], settings)

    assert [p.name for p in found] == ["msa.pdf", "second.pdf"]


def test_collect_paths_defaults_to_the_raw_dir(raw, settings):
    assert [p.name for p in collect_paths([], settings)] == ["msa.pdf"]


# --------------------------------------------------------------------------
# The sample contract, ingested for real
# --------------------------------------------------------------------------


@needs_sample
def test_the_sample_contract_ingests_to_the_measured_shape(conn, embedder, settings):
    result = ingest_file(SAMPLE_PDF, conn, embedder, settings)

    assert result.status == "ingested"
    assert result.chunks == 102
    assert result.pages == 21
    assert result.spine_source == "headings"
    assert counts(conn)["chunks"] == counts(conn)["vec"] == counts(conn)["fts"] == 102


@needs_sample
def test_the_sample_contracts_page_ranges_reach_the_database(conn, embedder, settings):
    """Eleven chunks sit on a clause or a table the parser rejoined across a
    page break; a citation for one of them must say p.N-M."""
    ingest_file(SAMPLE_PDF, conn, embedder, settings)

    spanning = conn.execute(
        "SELECT page, page_end, page_label, page_label_end FROM chunks "
        "WHERE page_end IS NOT NULL"
    ).fetchall()

    assert len(spanning) == 11
    assert all(r["page_end"] == r["page"] + 1 for r in spanning)
    assert all(r["page_label_end"] and r["page_label_end"] != r["page_label"] for r in spanning)
