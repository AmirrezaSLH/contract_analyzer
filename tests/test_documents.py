"""The document catalogue: the outline a picker shows, and a delete that leaves
nothing behind.

Two claims worth a test, and one of them is about SQLite rather than about this
code:

**Deleting a document empties all four tables.** `DELETE FROM documents` is one
statement; `chunks` goes by cascade, and the FTS and vec rows go by the
`chunks_ad` trigger. But SQLite's manual only promises that triggers fire for a
*direct* delete -- for rows removed by a foreign-key action it makes trigger
firing depend on `recursive_triggers`, which is off by default. So the outcome
is asserted here rather than assumed at the call site. If a future SQLite
changes its mind, this test fails and `delete_document` grows two explicit
statements; nothing else in the codebase has to notice.

**The outline is built from the chunks, not from the parser.** A section that
produced no chunk is unreachable by retrieval, and a picker that lists it
promises what the index cannot keep. The grouping is also what makes a section
split across four chunks appear once, with the page range of all four.

Everything here is offline: rows are written by hand, so no PDF, no embedder
and no key are involved. The one test that reads a real contract's outline uses
the shared `ingested_sample` corpus and is skipped when the sample is absent.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from contract_analyzer.config import Settings
from contract_analyzer.db import get_db
from contract_analyzer.documents import (
    delete_document,
    document_sections,
    get_document,
    list_documents,
)

DIM = 4


@pytest.fixture
def raw_dir(tmp_path) -> Path:
    """Where uploads land. A sibling of it stands in for "a file the caller
    pointed us at", which `delete_document` must leave alone."""
    path = tmp_path / "raw"
    path.mkdir()
    return path


@pytest.fixture
def settings(tmp_path, raw_dir) -> Settings:
    return Settings(
        embedding_provider="fake",
        embedding_model=None,
        embedding_dim=DIM,
        db_path=tmp_path / "contracts.db",
        raw_dir=raw_dir,
        assets_dir=tmp_path / "assets",
        log_file=None,
    )


@pytest.fixture
def store(settings, raw_dir):
    """A database with two documents written by hand. Function-scoped: the
    delete tests mutate it."""
    conn = get_db(settings)
    _write_document(conn, raw_dir, doc_id=1, name="alpha.pdf", pages=2, sections=_ALPHA)
    _write_document(conn, raw_dir, doc_id=2, name="beta.pdf", pages=1, sections=_BETA)
    yield conn
    conn.close()


#: (breadcrumb, page_label, page_label_end, text) per chunk, in document order.
_ALPHA = [
    (["6. Identity", "6.6 Password Management"], "9", "9", "Passwords rotate every 90 days."),
    (["6. Identity", "6.6 Password Management"], "9", "10", "Break-glass accounts are vaulted."),
    (["6. Identity", "6.7 Multi-Factor"], "10", "", "MFA is required for administrators."),
    (["Exhibit G"], "20", "", "GOV-01 Supplier shall maintain a register."),
]
_BETA = [
    (["1. Term"], "1", "", "This agreement runs for three years."),
]


def _write_document(conn, raw_dir, *, doc_id, name, pages, sections):
    raw = raw_dir / name
    raw.write_bytes(b"%PDF-1.4 not really a pdf")
    conn.execute(
        "INSERT INTO documents (id, path, filename, content_hash, page_count, spine_source) "
        "VALUES (?, ?, ?, ?, ?, 'headings')",
        (doc_id, str(raw), name, f"hash-{doc_id}", pages),
    )
    for ordinal, (path, label, label_end, text) in enumerate(sections):
        cursor = conn.execute(
            "INSERT INTO chunks (document_id, ordinal, content, page, page_label, "
            "page_label_end, section, section_path, embedding_model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'fake-hash')",
            (doc_id, ordinal, text, 0, label, label_end, path[-1], json.dumps(path)),
        )
        conn.execute(
            "INSERT INTO chunks_vec (chunk_id, document_id, embedding) VALUES (?, ?, ?)",
            (cursor.lastrowid, doc_id, struct.pack(f"{DIM}f", *([0.5] * DIM))),
        )
    conn.commit()


def _counts(conn, document_id):
    chunks = conn.execute(
        "SELECT count(*) FROM chunks WHERE document_id = ?", (document_id,)
    ).fetchone()[0]
    vectors = conn.execute(
        "SELECT count(*) FROM chunks_vec WHERE document_id = ?", (document_id,)
    ).fetchone()[0]
    fts = conn.execute(
        "SELECT count(*) FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
        "WHERE c.document_id = ?",
        (document_id,),
    ).fetchone()[0]
    return chunks, vectors, fts


# --------------------------------------------------------------------------
# Listing and lookup
# --------------------------------------------------------------------------


def test_list_returns_every_document_with_its_chunk_count(store):
    listed = {d.filename: d for d in list_documents(store)}
    assert set(listed) == {"alpha.pdf", "beta.pdf"}
    assert listed["alpha.pdf"].chunks == len(_ALPHA)
    assert listed["beta.pdf"].chunks == len(_BETA)
    assert listed["alpha.pdf"].page_count == 2
    assert listed["alpha.pdf"].spine_source == "headings"


def test_list_is_newest_first_even_within_one_second(store):
    """`ingested_at` has one-second resolution, so `id` has to break the tie --
    otherwise two uploads in the same second come back in an arbitrary order and
    the client that just uploaded cannot find its own document."""
    assert [d.document_id for d in list_documents(store)] == [2, 1]


def test_list_honours_limit(store):
    assert len(list_documents(store, limit=1)) == 1


def test_get_returns_none_for_an_unknown_id(store):
    """None, not an exception: the API turns it into a 404 with a hint, the CLI
    into a message. An exception would make both write a try block."""
    assert get_document(store, 999) is None
    assert get_document(store, 1).filename == "alpha.pdf"


# --------------------------------------------------------------------------
# The outline
# --------------------------------------------------------------------------


def test_consecutive_chunks_of_one_section_collapse_into_one_entry(store):
    sections = document_sections(store, 1)
    assert [s.title for s in sections] == [
        "6.6 Password Management",
        "6.7 Multi-Factor",
        "Exhibit G",
    ]
    passwords = sections[0]
    assert passwords.chunks == 2
    assert passwords.path == ["6. Identity", "6.6 Password Management"]
    assert passwords.depth == 2


def test_page_display_spans_the_whole_section(store):
    """Two chunks, the second ending on page 10: the section reads `9-10`, the
    same form a citation prints."""
    sections = document_sections(store, 1)
    assert sections[0].page_display == "9-10"
    assert sections[1].page_display == "10"


def test_sections_are_scoped_to_one_document(store):
    assert [s.title for s in document_sections(store, 2)] == ["1. Term"]
    assert document_sections(store, 999) == []


# --------------------------------------------------------------------------
# Delete
# --------------------------------------------------------------------------


def test_delete_empties_chunks_vectors_and_fts(store, settings):
    """The claim about SQLite, not about this code: a cascade fires the
    `chunks_ad` trigger, so one statement clears all four tables."""
    assert _counts(store, 1) == (4, 4, 4)

    assert delete_document(store, 1, settings) is True

    assert _counts(store, 1) == (0, 0, 0)
    assert store.execute("SELECT count(*) FROM documents WHERE id = 1").fetchone()[0] == 0


def test_delete_leaves_the_other_document_alone(store, settings):
    delete_document(store, 1, settings)
    assert _counts(store, 2) == (1, 1, 1)
    assert [d.document_id for d in list_documents(store)] == [2]


def test_delete_removes_the_raw_file(store, settings, raw_dir):
    raw = raw_dir / "alpha.pdf"
    assert raw.exists()
    delete_document(store, 1, settings)
    assert not raw.exists()


def test_delete_can_keep_the_raw_file(store, settings, raw_dir):
    delete_document(store, 1, settings, remove_file=False)
    assert (raw_dir / "alpha.pdf").exists()


def test_delete_keeps_a_file_that_lives_outside_raw_dir(store, settings, tmp_path):
    """`make ingest F=data/samples/...` points at a committed fixture. A
    catalogue delete has no business removing a file it did not put there."""
    fixture = tmp_path / "beta.pdf"
    fixture.write_bytes(b"%PDF-1.4")
    store.execute("UPDATE documents SET path = ? WHERE id = 2", (str(fixture),))
    store.commit()

    assert delete_document(store, 2, settings) is True
    assert fixture.exists()


def test_delete_reports_an_unknown_id_rather_than_raising(store, settings):
    assert delete_document(store, 999, settings) is False


def test_deleted_text_is_gone_from_the_full_text_index(store, settings):
    """The sharp end of the trigger claim: an external-content FTS5 index keeps
    no copy of the text, so a stale entry would point at a deleted rowid and a
    later search would raise or return a hole."""
    hits = store.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'vaulted'")
    assert hits.fetchone()[0] == 1
    delete_document(store, 1, settings)
    hits = store.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'vaulted'")
    assert hits.fetchone()[0] == 0


# --------------------------------------------------------------------------
# Against a real contract
# --------------------------------------------------------------------------


def test_the_sample_contracts_outline_is_ordered_and_paged(ingested_sample):
    """Document order, not alphabetical: a reader picking a section scrolls the
    contract, and the first entry must be the first section."""
    sections = document_sections(ingested_sample.conn, ingested_sample.sample_id)

    assert len(sections) > 20
    assert all(s.chunks >= 1 for s in sections)
    assert any(s.title.startswith("6.6") for s in sections)
    assert any(s.title.startswith("Exhibit G") for s in sections)
    assert any(s.path[:1] == ["Exhibit G — Security Schedule (Numbered Requirements)"]
               and s.depth == 2 for s in sections)

    titles = [s.title for s in sections]
    passwords = titles.index("6.6 Password Management Standard")
    exhibit_g = next(i for i, t in enumerate(titles) if t.startswith("Exhibit G"))
    assert passwords < exhibit_g

    total = sum(s.chunks for s in sections)
    assert total == get_document(ingested_sample.conn, ingested_sample.sample_id).chunks
