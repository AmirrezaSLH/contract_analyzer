"""The rule that keeps two embedding models out of the same corpus.

Vectors from two different models are points in unrelated spaces. Mixed, they
do not error, they do not look wrong, and they do not return nothing -- they
return a ranking that is plausible and meaningless. Nothing about the symptom
points at the cause, which is why this check is up front on both paths rather
than a note in the README.

There are two paths, and they need different sentences:

* **Writing** (`check_embedding_model`) -- refuse to *add* vectors to a corpus
  that another model built. The fix is to restore the old setting or rebuild.
* **Reading** (`check_query_model`) -- refuse to *answer* against it. The fix
  is the same, but the reason to state is that every ranking returned would be
  noise, which is not obvious from a results list that looks fine.

This module holds no connection and imports nothing from `ingest` or
`retrieval`: the read path must not import the write path, and the guard is the
one piece both of them need.
"""

from __future__ import annotations

import sqlite3

from .base import Embedder


class ModelMismatch(RuntimeError):
    """The corpus was embedded by a different model than the one configured."""


def stored_embedding_models(conn: sqlite3.Connection) -> list[str]:
    """Every model named in `chunks`. Empty means an empty corpus, not an error."""
    rows = conn.execute("SELECT DISTINCT embedding_model FROM chunks").fetchall()
    return sorted(row[0] for row in rows)


def _others(conn: sqlite3.Connection, embedder: Embedder) -> list[str]:
    return [name for name in stored_embedding_models(conn) if name != embedder.name]


def check_embedding_model(conn: sqlite3.Connection, embedder: Embedder) -> None:
    """Refuse to add vectors to a corpus embedded by a different model."""
    others = _others(conn, embedder)
    if not others:
        return
    raise ModelMismatch(
        f"the database already holds chunks embedded by {', '.join(others)}, "
        f"but EMBEDDING_PROVIDER/EMBEDDING_MODEL resolves to {embedder.name}. "
        "Vectors from two models are not comparable: either restore the previous "
        "setting, or delete the database and re-ingest every contract."
    )


def check_query_model(conn: sqlite3.Connection, embedder: Embedder) -> None:
    """Refuse to answer a query the corpus cannot be compared against.

    Called once per `retrieve()`, *before* the query is embedded: the check is
    a single SELECT over a tiny distinct set, and the embedding it saves is an
    HTTP round trip that costs money.
    """
    others = _others(conn, embedder)
    if not others:
        return
    raise ModelMismatch(
        f"this query would be embedded by {embedder.name}, but the corpus was "
        f"embedded by {', '.join(others)}. The two are points in unrelated spaces, "
        "so every ranking returned would be noise -- and it would look like a "
        "normal list of results. Restore the previous EMBEDDING_PROVIDER/"
        "EMBEDDING_MODEL, or re-ingest with this one."
    )


__all__ = [
    "ModelMismatch",
    "check_embedding_model",
    "check_query_model",
    "stored_embedding_models",
]
