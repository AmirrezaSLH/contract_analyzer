"""One `retrieve()`: two rankings, fused, hydrated, scoped to one contract.

Reciprocal Rank Fusion is the whole trick, and it is four lines. Each retriever
contributes `1 / (rrf_k + rank)` per chunk; the two lists are added. Ranks, not
scores -- an L2 distance and a BM25 score are not on the same scale and no
weighting of them is defensible without a labelled set to tune on. What RRF
says instead is: *a chunk both retrievers thought was reasonable beats one that
a single retriever loved*. On compliance questions that is exactly right, since
the two sides fail in opposite directions -- BM25 misses "secure admin pathway"
for "bastion host", vectors miss `PASS-02`.

`candidates` is deeper than `top_k` on purpose: fusion can only rank a chunk
that at least one retriever returned, so the pool has to be wider than the
answer. `rrf_k=60` is the value from the original paper; it flattens the
difference between rank 1 and rank 2 so that agreement outweighs position.

Scoping is the one thing this file will not do implicitly. `document_id` is a
required argument, and searching the whole corpus is spelled `ALL_DOCUMENTS`:
in a product where every analysis is about the contract someone just uploaded,
a forgotten scope does not raise -- it answers with another contract's clause,
in a well-formed citation.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Mapping, Sequence

from ..config import RetrievalMode, Settings, get_settings
from ..embeddings.base import Embedder
from ..embeddings.guard import check_query_model
from ..logger import get_logger, span
from .base import (
    ALL_DOCUMENTS,
    DocumentScope,
    RetrievalResult,
    RetrievedChunk,
    hydrate,
    similarity_from_distance,
)
from .keyword import keyword_search
from .vector import embed_question, vector_search

log = get_logger(__name__)

#: The modes that cannot run without an embedder. `keyword` is deliberately not
#: one of them: it is the mode that works offline, with no key and no API bill,
#: and on identifier-heavy contract text it is a genuine fallback rather than a
#: degraded one.
NEEDS_EMBEDDER = frozenset({"hybrid", "vector"})


def rrf_fuse(
    rankings: Mapping[str, Sequence[int]],
    *,
    rrf_k: int = 60,
) -> list[tuple[int, float]]:
    """Fuse ranked id lists into one ranking, best first.

    Ties break on ascending `chunk_id`. That is not cosmetic: two chunks a
    single retriever returned at adjacent ranks tie exactly whenever the other
    retriever returned neither, and without a deterministic tie-break the eval
    harness reports a different hit@5 on identical data.
    """
    scores: dict[int, float] = {}
    for ids in rankings.values():
        for rank, chunk_id in enumerate(ids, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (rrf_k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def retrieve(
    question: str,
    conn: sqlite3.Connection,
    embedder: Embedder | None = None,
    settings: Settings | None = None,
    *,
    document_id: DocumentScope,
    mode: RetrievalMode | None = None,
    top_k: int | None = None,
    candidates: int | None = None,
) -> RetrievalResult:
    """Rank the chunks that answer `question`, within one contract.

    `document_id` is an id, or `ALL_DOCUMENTS` for the whole corpus. `mode`,
    `top_k` and `candidates` fall back to the settings, so a caller states only
    what it wants to change.

    Raises `ValueError` if a mode that embeds is asked for without an embedder,
    and `ModelMismatch` -- before the question is embedded, which is an HTTP
    round trip and a charge -- if the corpus was built by a different model.
    """
    settings = settings or get_settings()
    mode = mode or settings.retrieval_mode
    top_k = top_k or settings.retrieval_top_k
    # A pool narrower than the answer would silently truncate it.
    candidates = max(candidates or settings.retrieval_candidates, top_k)
    scope = None if document_id is ALL_DOCUMENTS else int(document_id)

    if mode in NEEDS_EMBEDDER:
        if embedder is None:
            raise ValueError(
                f"mode={mode!r} embeds the question and needs an embedder; "
                "pass one, or use mode='keyword', which runs offline."
            )
        check_query_model(conn, embedder)

    rankings: dict[str, list[int]] = {}
    distances: dict[int, float] = {}
    keyword_scores: dict[int, float] = {}
    timings: dict[str, float] = {}

    with span("retrieve", log, mode=mode, document_id=scope, top_k=top_k) as bag:
        if mode in NEEDS_EMBEDDER:
            assert embedder is not None  # checked above
            started = time.perf_counter()
            vector = embed_question(embedder, question)
            timings["embed_ms"] = _ms(started)
            started = time.perf_counter()
            hits = vector_search(conn, vector, k=candidates, document_id=scope)
            timings["vector_ms"] = _ms(started)
            rankings["vector"] = [chunk_id for chunk_id, _ in hits]
            distances = dict(hits)

        if mode in {"hybrid", "keyword"}:
            started = time.perf_counter()
            hits = keyword_search(conn, question, k=candidates, document_id=scope)
            timings["keyword_ms"] = _ms(started)
            rankings["keyword"] = [chunk_id for chunk_id, _ in hits]
            keyword_scores = dict(hits)

        ranked = _rank(mode, rankings, distances, keyword_scores, rrf_k=settings.rrf_k)[:top_k]
        scores = dict(ranked)
        similarities = {
            chunk_id: similarity_from_distance(distance)
            for chunk_id, distance in distances.items()
            if chunk_id in scores
        }
        started = time.perf_counter()
        chunks = hydrate(conn, scores, similarities=similarities, ranks=_ranks(rankings, scores))
        timings["hydrate_ms"] = _ms(started)
        bag["results"] = len(chunks)

    return RetrievalResult(
        question=question,
        mode=mode,
        document_id=scope,
        chunks=chunks,
        candidates=candidates,
        top_k=top_k,
        timings=timings,
    )


def _rank(
    mode: str,
    rankings: Mapping[str, Sequence[int]],
    distances: Mapping[int, float],
    keyword_scores: Mapping[int, float],
    *,
    rrf_k: int,
) -> list[tuple[int, float]]:
    """The single ranking each mode produces, with its own score attached.

    Only `hybrid` fuses. A single-retriever mode keeps that retriever's own
    number -- a cosine similarity or a BM25 score is worth reading, and an RRF
    score over one list is just `1/(60+rank)` said twice.
    """
    if mode == "vector":
        return [
            (chunk_id, similarity_from_distance(distances[chunk_id]))
            for chunk_id in rankings.get("vector", ())
        ]
    if mode == "keyword":
        return [(chunk_id, keyword_scores[chunk_id]) for chunk_id in rankings.get("keyword", ())]
    return rrf_fuse(rankings, rrf_k=rrf_k)


def _ranks(
    rankings: Mapping[str, Sequence[int]], keep: Mapping[int, float]
) -> dict[int, dict[str, int]]:
    """Where each surviving chunk placed, per retriever. 1-based."""
    ranks: dict[int, dict[str, int]] = {chunk_id: {} for chunk_id in keep}
    for name, ids in rankings.items():
        for rank, chunk_id in enumerate(ids, start=1):
            if chunk_id in ranks:
                ranks[chunk_id][name] = rank
    return ranks


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


__all__ = ["NEEDS_EMBEDDER", "RetrievalResult", "RetrievedChunk", "retrieve", "rrf_fuse"]
