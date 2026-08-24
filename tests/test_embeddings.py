"""Embedders: the width check, the model guard, and the one retry policy.

Two claims here need evidence rather than a docstring. The first is that
`FakeEmbedder` is deterministic across processes, because "re-ingesting an
unchanged file is a no-op" is only testable if the same text gives the same
vector tomorrow. The second is that the OpenAI SDK's own retries are off and
the transport in `http_client.py` is the only loop that runs -- asserted by
counting requests through a `MockTransport`, which is the seam where this step
and the HTTP client meet.
"""

from __future__ import annotations

import sqlite3

import httpx2 as httpx
import pytest

from contract_analyzer import http_client as H
from contract_analyzer.config import Settings
from contract_analyzer.db import apply_schema, connect
from contract_analyzer.embeddings import openai as openai_module
from contract_analyzer.embeddings.base import (
    BaseEmbedder,
    DimensionMismatch,
    Embedder,
    EmbedderUnavailable,
    get_embedder,
    normalize,
)
from contract_analyzer.embeddings.fake import FakeEmbedder
from contract_analyzer.embeddings.guard import (
    ModelMismatch,
    check_embedding_model,
    check_query_model,
    stored_embedding_models,
)

DIM = 64


def settings(**overrides) -> Settings:
    overrides.setdefault("embedding_provider", "fake")
    overrides.setdefault("embedding_dim", DIM)
    return Settings(**overrides)


# --------------------------------------------------------------------------
# The fake embedder -- what the offline suite and the keyless demo rest on
# --------------------------------------------------------------------------


def test_the_fake_embedder_is_deterministic_and_unit_length():
    a = FakeEmbedder(settings())
    b = FakeEmbedder(settings())
    text = "Vendor shall rotate privileged credentials every ninety days."

    assert a.embed_query(text) == b.embed_query(text)
    assert len(a.embed_query(text)) == DIM
    assert sum(v * v for v in a.embed_query(text)) == pytest.approx(1.0)


def test_the_fake_embedder_names_itself_in_the_row_it_writes():
    """A database built with hashed vectors has to announce itself."""
    embedder = FakeEmbedder(settings())

    assert embedder.name == f"fake-hash-{DIM}"
    assert embedder.dim == DIM


def test_the_fake_embedder_counts_the_texts_it_was_asked_for():
    """The counter idempotency is proved with: a skipped file must not add to it."""
    embedder = FakeEmbedder(settings())
    embedder.embed_documents(["one", "two", "three"])
    embedder.embed_query("four")

    assert embedder.calls == 4


def test_different_words_give_different_vectors():
    embedder = FakeEmbedder(settings())

    assert embedder.embed_query("password rotation") != embedder.embed_query("asset inventory")


def test_an_empty_list_is_not_a_request():
    embedder = FakeEmbedder(settings())

    assert embedder.embed_documents([]) == []
    assert embedder.calls == 0


def test_the_fake_embedder_satisfies_the_protocol():
    assert isinstance(FakeEmbedder(settings()), Embedder)


# --------------------------------------------------------------------------
# normalize
# --------------------------------------------------------------------------


def test_normalize_scales_to_unit_length():
    assert normalize([3.0, 4.0]) == [0.6, 0.8]


def test_normalize_leaves_a_zero_vector_alone():
    """A text with no recognised words must not become a vector of NaNs."""
    assert normalize([0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0]


# --------------------------------------------------------------------------
# The width check
# --------------------------------------------------------------------------


class _WrongWidth(BaseEmbedder):
    def _embed(self, texts, *, query):
        return [[0.1] * 7 for _ in texts]


def test_a_wrong_width_is_caught_on_the_first_vector():
    """Before any write: vec0's own error names neither the model nor the cause."""
    embedder = _WrongWidth("bad-model", DIM)

    with pytest.raises(DimensionMismatch) as excinfo:
        embedder.embed_documents(["anything"])

    assert "bad-model" in str(excinfo.value)
    assert str(DIM) in str(excinfo.value)


class _WrongCount(BaseEmbedder):
    def _embed(self, texts, *, query):
        return [[0.0] * DIM]


def test_a_short_response_is_caught_before_it_is_zipped_to_chunks():
    with pytest.raises(DimensionMismatch):
        _WrongCount("truncating-model", DIM).embed_documents(["a", "b", "c"])


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------


def test_get_embedder_builds_the_configured_provider():
    embedder = get_embedder(settings())

    assert isinstance(embedder, FakeEmbedder)


def test_get_embedder_refuses_a_width_the_provider_cannot_emit():
    """config.py checks intent; this is that check reaching the caller."""
    with pytest.raises(ValueError, match="384"):
        get_embedder(settings(embedding_provider="local", embedding_dim=512))


def test_the_local_extra_is_optional():
    """Importing the package must not need the ~800 MB torch install."""
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        with pytest.raises(EmbedderUnavailable, match=r"\[local\]"):
            get_embedder(settings(embedding_provider="local", embedding_dim=384))
    else:  # pragma: no cover - only when the extra is installed
        pytest.skip("sentence-transformers is installed")


# --------------------------------------------------------------------------
# The model guard
# --------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "contracts.db")
    apply_schema(connection, DIM)
    yield connection
    connection.close()


def _row(conn: sqlite3.Connection, model: str) -> None:
    conn.execute("INSERT INTO documents (path, filename, content_hash) VALUES (?, ?, ?)",
                 (f"{model}.pdf", f"{model}.pdf", model))
    document_id = conn.execute("SELECT id FROM documents ORDER BY id DESC").fetchone()[0]
    conn.execute(
        "INSERT INTO chunks (document_id, ordinal, content, embedding_model) VALUES (?, ?, ?, ?)",
        (document_id, 0, "6.6 Password Management Standard", model),
    )
    conn.commit()


def test_an_empty_corpus_matches_every_model(conn):
    embedder = FakeEmbedder(settings())

    assert stored_embedding_models(conn) == []
    check_embedding_model(conn, embedder)
    check_query_model(conn, embedder)


def test_writing_into_another_models_corpus_is_refused(conn):
    _row(conn, "text-embedding-3-small")

    with pytest.raises(ModelMismatch, match="text-embedding-3-small"):
        check_embedding_model(conn, FakeEmbedder(settings()))


def test_querying_another_models_corpus_is_refused(conn):
    """The ranking would be noise and would look like a normal list of results."""
    _row(conn, "text-embedding-3-small")

    with pytest.raises(ModelMismatch, match="noise"):
        check_query_model(conn, FakeEmbedder(settings()))


def test_the_same_model_is_allowed_to_add_to_its_own_corpus(conn):
    embedder = FakeEmbedder(settings())
    _row(conn, embedder.name)

    check_embedding_model(conn, embedder)
    check_query_model(conn, embedder)


# --------------------------------------------------------------------------
# OpenAI -- the seam between this step and http_client.py
# --------------------------------------------------------------------------


class _Upstream:
    """A scripted embeddings endpoint. Each call pops the next status."""

    def __init__(self, *statuses: int) -> None:
        self.statuses = list(statuses)
        self.calls = 0
        self.payloads: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        import json

        self.calls += 1
        self.payloads.append(json.loads(request.content))
        status = self.statuses.pop(0) if self.statuses else 200
        if status == 200:
            n = len(self.payloads[-1]["input"])
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "model": "text-embedding-3-small",
                    "data": [
                        {"object": "embedding", "index": i, "embedding": [0.0] * (DIM - 1) + [1.0]}
                        for i in range(n)
                    ],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                },
            )
        return httpx.Response(status, json={"error": {"message": "scripted", "type": "x"}})


def _embedder(monkeypatch, upstream: _Upstream, retries: int = 3):
    client = httpx.Client(
        transport=H.RetryingTransport(
            httpx.MockTransport(upstream), retries=retries, sleep=lambda s: None
        )
    )
    monkeypatch.setattr(openai_module, "get_http_client", lambda settings: client)
    return openai_module.OpenAIEmbedder(
        settings(embedding_provider="openai", openai_api_key="sk-test")
    )


def test_a_503_is_retried_exactly_once_by_the_transport(monkeypatch):
    """The proof that the SDK's own retries are off.

    With both loops live this would be four requests, not two, and the only
    symptom in production would be a bill and a rate limit.
    """
    upstream = _Upstream(503, 200)
    embedder = _embedder(monkeypatch, upstream)

    vectors = embedder.embed_documents(["6.6 Password Management Standard"])

    assert upstream.calls == 2
    assert len(vectors) == 1 and len(vectors[0]) == DIM


def test_an_exhausted_retry_policy_reaches_the_caller_as_one_line(monkeypatch):
    """Not an SDK connection error wrapping ours: the message names the URL."""
    upstream = _Upstream(503, 503)
    embedder = _embedder(monkeypatch, upstream, retries=1)

    with pytest.raises(H.HttpFailure) as excinfo:
        embedder.embed_documents(["anything"])

    assert upstream.calls == 2
    assert "embeddings" in str(excinfo.value)
    assert excinfo.value.status == 503


def test_a_401_is_a_configuration_error_not_a_retry(monkeypatch):
    upstream = _Upstream(401)
    embedder = _embedder(monkeypatch, upstream)

    with pytest.raises(EmbedderUnavailable, match="OPENAI_API_KEY"):
        embedder.embed_documents(["anything"])

    assert upstream.calls == 1, "a bad key does not get better by asking again"


def test_the_matryoshka_width_is_requested_not_assumed(monkeypatch):
    upstream = _Upstream(200)
    embedder = _embedder(monkeypatch, upstream)

    embedder.embed_documents(["anything"])

    assert upstream.payloads[0]["dimensions"] == DIM
    assert upstream.payloads[0]["model"] == "text-embedding-3-small"


def test_a_missing_key_is_refused_before_any_request(monkeypatch):
    with pytest.raises(EmbedderUnavailable, match="fake"):
        openai_module.OpenAIEmbedder(settings(embedding_provider="openai", openai_api_key=None))


def test_an_empty_chunk_is_sent_as_a_space_rather_than_failing_the_contract(monkeypatch):
    upstream = _Upstream(200)
    embedder = _embedder(monkeypatch, upstream)

    embedder.embed_documents(["", "real text"])

    assert upstream.payloads[0]["input"] == [" ", "real text"]


def test_the_reported_usage_is_kept_and_summed_across_batches(monkeypatch):
    """The one provider here that bills is the one that reports usage, and
    `ingest.embed` prices its span from this. Summed across round trips, so a
    contract embedded in three batches still costs one number."""
    upstream = _Upstream(200, 200)
    embedder = _embedder(monkeypatch, upstream)
    monkeypatch.setattr(openai_module, "BATCH_SIZE", 1)

    embedder.embed_documents(["clause one", "clause two"])

    assert upstream.calls == 2
    assert embedder.last_tokens == 2


def test_last_tokens_is_reset_per_call_not_accumulated_forever(monkeypatch):
    """It is what the *most recent* call billed. A running total would make the
    second document in a directory ingest look twice as expensive."""
    embedder = _embedder(monkeypatch, _Upstream())

    embedder.embed_documents(["clause one"])
    embedder.embed_documents(["clause two"])

    assert embedder.last_tokens == 1


def test_an_offline_embedder_reports_no_tokens_and_that_is_the_truth():
    """The local and fake embedders run in this process and bill nothing, so
    zero is the honest number rather than a missing one."""
    embedder = FakeEmbedder(settings())

    embedder.embed_documents(["clause one", "clause two"])

    assert embedder.last_tokens == 0
