"""Structural lookup: give me section 6.6, or all of Exhibit G.

Phase B's router knows which section a criterion lives in long before it knows
which sentence answers it ("password management -> 6.6, Exhibit G"). Turning
that into a similarity search would be a worse way to ask a question this
module answers in SQL, with no embedder, no key and no ranking.

A separate module rather than a fourth mode of `retrieve()`: it takes no
question, returns no scores, and its results are in *document* order, which is
what makes a whole section readable.
"""

from __future__ import annotations

import sqlite3

from .base import SELECT_CHUNK, RetrievedChunk, chunk_from_row

#: LIKE wildcards, escaped so a caller's pattern is read literally. Without
#: this, `_` matches any character -- `6_6` would find `6.6` and `676` alike --
#: and a pattern coming from an LLM router will contain one eventually.
_ESCAPE = "\\"


def retrieve_by_section(
    conn: sqlite3.Connection,
    document_id: int,
    pattern: str,
    *,
    limit: int = 20,
) -> list[RetrievedChunk]:
    """Chunks whose breadcrumb has a component starting with `pattern`.

    `pattern` is a plain prefix -- `"6.6"`, `"Exhibit G"` -- not a LIKE
    expression. It is anchored to the **start of a path component**, so `6.6`
    finds `6.6 Password Management Standard` at any depth and does not match
    `16.6 Force Majeure`; the anchor is the JSON quote that opens every
    component of `chunks.section_path`.

    Results are ordered by `ordinal`, so a section split across several chunks
    reads in the order the contract has it.
    """
    if not pattern.strip():
        return []
    sql = f"""
        {SELECT_CHUNK}
        WHERE c.document_id = ?
          AND c.section_path LIKE '%"' || ? || '%' ESCAPE '{_ESCAPE}'
        ORDER BY c.ordinal
        LIMIT ?
    """
    rows = conn.execute(sql, (document_id, _escape_like(pattern), limit))
    return [chunk_from_row(row) for row in rows]


def _escape_like(pattern: str) -> str:
    for char in (_ESCAPE, "%", "_"):
        pattern = pattern.replace(char, _ESCAPE + char)
    return pattern


__all__ = ["retrieve_by_section"]
