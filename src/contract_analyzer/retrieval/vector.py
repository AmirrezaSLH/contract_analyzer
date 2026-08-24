"""The KNN side: a question becomes a vector, `chunks_vec` returns its nearest.

Two things about this query are deliberate.

**`k` is mandatory.** A `vec0` KNN query without `k` (or a LIMIT) is an error,
not a full scan -- so there is no accidental way to ask for the whole corpus.

**The document filter is the partition key**, which sqlite-vec applies *before*
`k` rather than after. A scoped search therefore returns that contract's true
`k` nearest chunks. The alternative -- over-fetch `k * 4` globally and filter in
Python -- silently returns fewer than `k` results, or none, exactly when the
other contracts in the database are the more similar ones.
"""

from __future__ import annotations

import sqlite3

import sqlite_vec

from ..embeddings.base import Embedder

#: `chunk_id`, `distance` -- L2, so smaller is better. `base.similarity_from_distance`
#: turns it into the cosine number a reader expects to see.
Hit = tuple[int, float]


def vector_search(
    conn: sqlite3.Connection,
    vector: list[float],
    *,
    k: int,
    document_id: int | None = None,
) -> list[Hit]:
    """The `k` nearest chunks, nearest first; within one document when scoped."""
    if k <= 0:
        return []
    sql = "SELECT chunk_id, distance FROM chunks_vec WHERE embedding MATCH ? AND k = ?"
    params: list[object] = [sqlite_vec.serialize_float32(vector), k]
    if document_id is not None:
        sql += " AND document_id = ?"
        params.append(document_id)
    sql += " ORDER BY distance"
    return [(int(row["chunk_id"]), float(row["distance"])) for row in conn.execute(sql, params)]


def embed_question(embedder: Embedder, question: str) -> list[float]:
    """`embed_query`, never `embed_documents`.

    The asymmetry is the entire reason those are two methods: bge prefixes a
    query with an instruction that must never be prepended to a passage, and
    embedding a question as if it were a passage costs recall silently.
    """
    return embedder.embed_query(question)


__all__ = ["Hit", "embed_question", "vector_search"]
