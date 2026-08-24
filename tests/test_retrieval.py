"""Retrieval: scoping, fusion, citations, and the offline path.

The claim this step makes is not "results look good" -- with hashed `fake`
vectors that would be meaningless. It is three narrower claims, and each one
is falsifiable:

**A search scoped to a contract cannot see another contract.** Two documents
are ingested into one database, one of them a decoy that repeats the sample's
password vocabulary verbatim, so it holds the *nearest* vectors to a password
question. A global KNN would return the decoy; a scoped one must return the
sample's own k nearest, not the remainder of an over-fetch.

**Fusion is deterministic and rewards agreement.** Ties break on `chunk_id`,
because otherwise two chunks a single retriever returned at adjacent ranks tie
exactly and an eval harness reports a different hit@5 on identical data.

**A citation can be checked.** The section and the printed page range come
back with the chunk, and a table's text keeps the breadcrumb that says which
section's requirements its rows are.

The end-to-end keyword assertions double as regression tests for two earlier
fixes: `GOV- 01` rejoined by the parser, and every FTS5 term quoted so a bare
hyphen is not read as syntax.
"""

from __future__ import annotations

import pytest

from contract_analyzer.embeddings.fake import FakeEmbedder
from contract_analyzer.embeddings.guard import ModelMismatch
from contract_analyzer.retrieval import (
    ALL_DOCUMENTS,
    escape_query,
    hydrate,
    keyword_search,
    retrieve,
    retrieve_by_section,
    rrf_fuse,
    similarity_from_distance,
    vector_search,
)
from contract_analyzer.retrieval.base import RetrievedChunk
from contract_analyzer.retrieval.vector import embed_question

PASSWORD_QUESTION = "password rotation break-glass credentials"


# --------------------------------------------------------------------------
# Unit: no database, no embedder
# --------------------------------------------------------------------------


def test_rrf_prefers_agreement_over_enthusiasm():
    """A chunk both retrievers put third beats one a single retriever put first."""
    fused = dict(rrf_fuse({"vector": [10, 20, 30], "keyword": [40, 50, 30]}))
    assert max(fused, key=lambda cid: fused[cid]) == 30
    assert fused[30] > fused[10]


def test_rrf_ties_break_on_chunk_id():
    """Determinism, not aesthetics: an eval harness must not drift on rerun."""
    ranked = rrf_fuse({"keyword": [77, 11]})
    assert [cid for cid, _ in rrf_fuse({"keyword": [11], "vector": [77]})] == [11, 77]
    assert [cid for cid, _ in ranked] == [77, 11]  # rank order still wins over id


def test_rrf_ignores_a_retriever_that_did_not_return_a_chunk():
    single = dict(rrf_fuse({"vector": [5]}))
    both = dict(rrf_fuse({"vector": [5], "keyword": [5]}))
    assert both[5] == pytest.approx(2 * single[5])


def test_rrf_k_flattens_the_top_of_the_ranking():
    steep = dict(rrf_fuse({"v": [1, 2]}, rrf_k=1))
    flat = dict(rrf_fuse({"v": [1, 2]}, rrf_k=60))
    assert steep[1] / steep[2] > flat[1] / flat[2]


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("GOV-01", '"gov 01"'),
        ("TLS 1.2", '"tls" OR "1 2"'),
        ("password rotation", '"password" OR "rotation"'),
        ("-- ?!", ""),
        ("", ""),
    ],
)
def test_escape_query_quotes_every_term(question, expected):
    """Bare punctuation is FTS5 syntax: `GOV-01` raises without the quotes."""
    assert escape_query(question) == expected


def test_escape_query_joins_with_or():
    """A ranker, not a filter: one absent word must not empty the result."""
    assert " OR " in escape_query("password rotation vault")
    assert " AND " not in escape_query("password rotation vault")


@pytest.mark.parametrize(
    ("distance", "expected"),
    [(0.0, 1.0), (2.0, 0.0), (1.4142135, 0.0), (3.0, 0.0), (-0.001, 1.0)],
)
def test_similarity_from_distance(distance, expected):
    assert similarity_from_distance(distance) == pytest.approx(expected, abs=1e-6)


def _chunk(**overrides) -> RetrievedChunk:
    fields = dict(
        chunk_id=1,
        document_id=1,
        ordinal=0,
        content="6.6 Password Management Standard\nPasswords rotate every 90 days.",
        filename="Sample Contract.pdf",
        page=8,
        page_label="9",
        section="6.6 Password Management Standard",
        section_path=["6. Identity and Access", "6.6 Password Management Standard"],
    )
    fields.update(overrides)
    return RetrievedChunk(**fields)


def test_page_display_shows_a_range_when_the_chunk_spans_a_break():
    assert _chunk().page_display == "9"
    assert _chunk(page_end=9, page_label_end="10").page_display == "9-10"
    # A page_end equal to the start is not a range: "p.9-9" is noise.
    assert _chunk(page_end=8, page_label_end="9").page_display == "9"


def test_citation_title_names_the_section_and_the_printed_page():
    assert _chunk(page_end=9, page_label_end="10").citation_title == (
        "Sample Contract.pdf — 6.6 Password Management Standard (p.9-10)"
    )


def test_citation_title_degrades_rather_than_faking_what_it_lacks():
    assert _chunk(section_path=[], section="").citation_title == "Sample Contract.pdf (p.9)"
    assert (
        _chunk(page_label="").citation_title
        == "Sample Contract.pdf — 6.6 Password Management Standard"
    )


def test_text_for_model_keeps_a_tables_breadcrumb():
    """The grid's cells never name the section the requirements belong to."""
    table = _chunk(element_type="table", payload="|Control|Owner|\n|---|---|\n|Rotation|CISO|")
    assert table.text_for_model().startswith("6. Identity and Access > 6.6 Password Management")
    assert "|Rotation|CISO|" in table.text_for_model()
    # A paragraph's content already opens with its breadcrumb; nothing is added.
    assert _chunk().text_for_model() == _chunk().content


def test_text_for_model_falls_back_to_content_without_a_payload():
    assert _chunk(element_type="table", payload=None).text_for_model() == _chunk().content


# --------------------------------------------------------------------------
# Against the two-document database
# --------------------------------------------------------------------------


def _document_of(corpus, chunk_id: int) -> int:
    row = corpus.conn.execute("SELECT document_id FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
    return row["document_id"]


def test_keyword_finds_the_hyphenated_identifier(ingested_sample):
    """`GOV-01` end to end: the parser's rejoin, the breadcrumb, the escaping."""
    result = retrieve(
        "GOV-01",
        ingested_sample.conn,
        ingested_sample.embedder,
        ingested_sample.settings,
        document_id=ingested_sample.sample_id,
        mode="keyword",
        top_k=5,
    )
    assert result, "GOV-01 must match the Exhibit G requirement row"
    top = result.chunks[0]
    assert top.element_type == "table"
    assert "Exhibit G" in top.breadcrumb and "G1. Governance" in top.breadcrumb
    assert "GOV-01" in top.content


def test_keyword_finds_the_password_standard(ingested_sample):
    result = retrieve(
        PASSWORD_QUESTION,
        ingested_sample.conn,
        ingested_sample.embedder,
        ingested_sample.settings,
        document_id=ingested_sample.sample_id,
        mode="keyword",
        top_k=3,
    )
    breadcrumbs = " | ".join(chunk.breadcrumb for chunk in result)
    assert "6.6 Password Management Standard" in breadcrumbs or "G3A" in breadcrumbs


@pytest.mark.parametrize("mode", ["hybrid", "vector", "keyword"])
def test_every_result_belongs_to_the_document_asked_for(ingested_sample, mode):
    result = retrieve(
        PASSWORD_QUESTION,
        ingested_sample.conn,
        ingested_sample.embedder,
        ingested_sample.settings,
        document_id=ingested_sample.sample_id,
        mode=mode,
        top_k=10,
    )
    assert result, "the sample contract has password clauses in every mode"
    assert result.document_id == ingested_sample.sample_id
    assert {chunk.document_id for chunk in result} == {ingested_sample.sample_id}


def test_a_term_unique_to_the_other_contract_returns_nothing(ingested_sample):
    scoped = retrieve(
        ingested_sample.decoy_token,
        ingested_sample.conn,
        ingested_sample.embedder,
        ingested_sample.settings,
        document_id=ingested_sample.sample_id,
        mode="keyword",
        top_k=5,
    )
    assert list(scoped) == []
    corpus = retrieve(
        ingested_sample.decoy_token,
        ingested_sample.conn,
        ingested_sample.embedder,
        ingested_sample.settings,
        document_id=ALL_DOCUMENTS,
        mode="keyword",
        top_k=5,
    )
    assert corpus and {chunk.document_id for chunk in corpus} == {ingested_sample.decoy_id}


def test_vector_scoping_is_exact_not_a_filtered_over_fetch(ingested_sample):
    """The decoy holds the nearest vectors; the scoped search still fills k.

    This is the whole reason `chunks_vec` is partitioned. Over-fetching k*4 and
    filtering in Python would return whatever was left over -- here, nothing.
    """
    vector = embed_question(ingested_sample.embedder, PASSWORD_QUESTION)
    k = 5
    globally = vector_search(ingested_sample.conn, vector, k=k)
    assert {_document_of(ingested_sample, cid) for cid, _ in globally} == {
        ingested_sample.decoy_id
    }, "the decoy must dominate an unscoped search, or this test proves nothing"

    scoped = vector_search(ingested_sample.conn, vector, k=k, document_id=ingested_sample.sample_id)
    assert len(scoped) == k
    assert all(_document_of(ingested_sample, cid) == ingested_sample.sample_id for cid, _ in scoped)
    assert [distance for _, distance in scoped] == sorted(d for _, d in scoped)


def test_hybrid_carries_the_rank_each_retriever_gave(ingested_sample):
    result = retrieve(
        "GOV-01 governance risk management",
        ingested_sample.conn,
        ingested_sample.embedder,
        ingested_sample.settings,
        document_id=ingested_sample.sample_id,
        mode="hybrid",
        top_k=5,
    )
    assert result
    assert any(chunk.ranks.get("keyword") for chunk in result)
    assert all(set(chunk.ranks) <= {"vector", "keyword"} for chunk in result)
    scores = [chunk.score for chunk in result]
    assert scores == sorted(scores, reverse=True)


def test_vector_mode_reports_similarity(ingested_sample):
    result = retrieve(
        PASSWORD_QUESTION,
        ingested_sample.conn,
        ingested_sample.embedder,
        ingested_sample.settings,
        document_id=ingested_sample.sample_id,
        mode="vector",
        top_k=3,
    )
    assert all(0.0 <= chunk.similarity <= 1.0 for chunk in result)
    assert all(chunk.similarity == pytest.approx(chunk.score) for chunk in result)


def test_a_chunk_carries_what_a_citation_needs(ingested_sample):
    result = retrieve(
        PASSWORD_QUESTION,
        ingested_sample.conn,
        ingested_sample.embedder,
        ingested_sample.settings,
        document_id=ingested_sample.sample_id,
        mode="keyword",
        top_k=3,
    )
    chunk = result.chunks[0]
    assert chunk.filename == "Sample Contract.pdf"
    # The sample is a Word contract with no outline: its sections were inferred,
    # and a surface must be able to say so without a second query.
    assert chunk.spine_source == "headings"
    assert chunk.breadcrumb and chunk.citation_title.startswith("Sample Contract.pdf — ")


# --------------------------------------------------------------------------
# Structural lookup
# --------------------------------------------------------------------------


def test_retrieve_by_section_anchors_to_a_path_component(ingested_sample):
    conn, doc = ingested_sample.conn, ingested_sample.sample_id
    six_six = retrieve_by_section(conn, doc, "6.6")
    assert len(six_six) == 1
    assert six_six[0].breadcrumb.endswith("6.6 Password Management Standard")
    # `16.6` is not a match for `6.6`: the anchor is the quote that opens a
    # component, which is the difference between a useful lookup and a trap.
    assert retrieve_by_section(conn, doc, "16.6") == []


def test_retrieve_by_section_returns_a_whole_exhibit_in_document_order(ingested_sample):
    chunks = retrieve_by_section(
        ingested_sample.conn, ingested_sample.sample_id, "Exhibit G", limit=50
    )
    assert len(chunks) == 15
    assert [chunk.ordinal for chunk in chunks] == sorted(chunk.ordinal for chunk in chunks)
    assert all("Exhibit G" in chunk.breadcrumb for chunk in chunks)


def test_retrieve_by_section_treats_like_wildcards_literally(ingested_sample):
    """`6_6` must not find `6.6`; a router's pattern is data, not SQL."""
    conn, doc = ingested_sample.conn, ingested_sample.sample_id
    assert retrieve_by_section(conn, doc, "6_6") == []
    assert retrieve_by_section(conn, doc, "%") == []
    assert retrieve_by_section(conn, doc, "  ") == []


def test_retrieve_by_section_is_scoped_and_needs_no_embedder(ingested_sample):
    assert retrieve_by_section(ingested_sample.conn, ingested_sample.decoy_id, "6.6") == []
    chunks = retrieve_by_section(ingested_sample.conn, ingested_sample.sample_id, "6.")
    assert chunks and {chunk.document_id for chunk in chunks} == {ingested_sample.sample_id}


def test_retrieve_by_section_honours_its_limit(ingested_sample):
    conn, doc = ingested_sample.conn, ingested_sample.sample_id
    assert retrieve_by_section(conn, doc, "", limit=3) == []
    assert len(retrieve_by_section(conn, doc, "Exhibit", limit=3)) == 3


# --------------------------------------------------------------------------
# Hydration, modes and guards
# --------------------------------------------------------------------------


def test_hydrate_returns_the_ranking_it_was_given(ingested_sample):
    """`WHERE id IN (...)` does not preserve order; the dict does."""
    ids = [row[0] for row in ingested_sample.conn.execute(
        "SELECT id FROM chunks WHERE document_id = ? ORDER BY ordinal LIMIT 4",
        (ingested_sample.sample_id,),
    )]
    ranking = {ids[2]: 0.9, ids[0]: 0.5, ids[3]: 0.1}
    chunks = hydrate(ingested_sample.conn, ranking)
    assert [chunk.chunk_id for chunk in chunks] == list(ranking)
    assert [chunk.score for chunk in chunks] == list(ranking.values())


def test_hydrate_drops_a_chunk_that_vanished_between_search_and_fetch(ingested_sample):
    missing = 10**9
    chunks = hydrate(ingested_sample.conn, {missing: 1.0})
    assert chunks == []


def test_keyword_mode_runs_without_an_embedder(ingested_sample):
    """The offline path: no key, no network, and still a real ranking."""
    result = retrieve(
        "GOV-01",
        ingested_sample.conn,
        None,
        ingested_sample.settings,
        document_id=ingested_sample.sample_id,
        mode="keyword",
    )
    assert result


@pytest.mark.parametrize("mode", ["hybrid", "vector"])
def test_a_mode_that_embeds_refuses_to_run_without_an_embedder(ingested_sample, mode):
    with pytest.raises(ValueError, match="embedder"):
        retrieve(
            "anything",
            ingested_sample.conn,
            None,
            ingested_sample.settings,
            document_id=ingested_sample.sample_id,
            mode=mode,
        )


def test_the_model_guard_runs_before_the_question_is_embedded(ingested_sample):
    """Embedding is an HTTP round trip and a charge; the check is one SELECT."""

    class _WrongModel(FakeEmbedder):
        def __init__(self, settings):
            super().__init__(settings)
            self.name = "some-other-model-64"

        def embed_query(self, text):  # pragma: no cover - must never be reached
            raise AssertionError("the corpus guard must run before embedding")

    with pytest.raises(ModelMismatch):
        retrieve(
            PASSWORD_QUESTION,
            ingested_sample.conn,
            _WrongModel(ingested_sample.settings),
            ingested_sample.settings,
            document_id=ingested_sample.sample_id,
            mode="vector",
        )


def test_top_k_deeper_than_candidates_widens_the_pool(ingested_sample):
    result = retrieve(
        PASSWORD_QUESTION,
        ingested_sample.conn,
        ingested_sample.embedder,
        ingested_sample.settings,
        document_id=ingested_sample.sample_id,
        mode="vector",
        top_k=8,
        candidates=3,
    )
    assert result.candidates == 8
    assert len(result.chunks) == 8


@pytest.mark.parametrize("mode", ["hybrid", "vector", "keyword"])
def test_an_empty_database_returns_an_empty_result(tmp_path, mode):
    from contract_analyzer.config import Settings
    from contract_analyzer.db import get_db

    settings = Settings(
        embedding_provider="fake",
        embedding_dim=32,
        db_path=tmp_path / "empty.db",
        log_file=None,
    )
    conn = get_db(settings)
    result = retrieve(
        "anything at all",
        conn,
        FakeEmbedder(settings),
        settings,
        document_id=ALL_DOCUMENTS,
        mode=mode,
    )
    assert not result and list(result) == [] and result.document_id is None


def test_keyword_search_survives_a_question_that_is_only_punctuation(ingested_sample):
    assert keyword_search(ingested_sample.conn, "?! -- ...", k=5) == []
    assert vector_search(ingested_sample.conn, [0.0] * 64, k=0) == []
