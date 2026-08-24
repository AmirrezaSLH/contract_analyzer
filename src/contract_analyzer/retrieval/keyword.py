"""The BM25 side: the words the contract actually uses.

This is the retriever that finds `GOV-01`, `TLS 1.2` and `PASS-02` -- the
identifiers a vector model turns into a vague neighbourhood of "security
control". On a compliance corpus it is not the weaker half of the pair.

**Every term is quoted.** FTS5's query language reads bare punctuation as
syntax: `GOV-01` is a column filter followed by a negation and raises
"fts5: syntax error near -", turning a user's question into a 500. Quoting
each term makes it a phrase, and since the tokenizer splits on the hyphen
anyway, `"gov 01"` is exactly what matches the cell containing `GOV-01`.

**Terms are joined with OR, not AND.** A retriever's job is to rank, not to
filter: a five-word question whose fifth word appears nowhere in the contract
should return the chunks matching the other four, ranked, rather than nothing
at all. BM25 already rewards the chunks that match more of them.
"""

from __future__ import annotations

import re
import sqlite3

#: `chunk_id`, `score` -- negated BM25, so **higher is better**, matching every
#: other score in this package.
Hit = tuple[int, float]

_WORD = re.compile(r"\w+", re.UNICODE)


def escape_query(question: str) -> str:
    """A user's question as a safe FTS5 MATCH expression.

    Returns `""` for a question with no word characters at all, which the
    caller reads as "nothing to search for" -- an empty MATCH is a syntax
    error, not an empty result set.
    """
    terms: list[str] = []
    for token in question.split():
        words = _WORD.findall(token.lower())
        if words:
            # One token becomes one phrase: `GOV-01` -> `"gov 01"`, which is
            # both safe and more precise than two independent terms.
            terms.append('"' + " ".join(words) + '"')
    return " OR ".join(terms)


def keyword_search(
    conn: sqlite3.Connection,
    question: str,
    *,
    k: int,
    document_id: int | None = None,
) -> list[Hit]:
    """The `k` best BM25 matches, best first; within one document when scoped.

    Unlike the vector side there is no filtering subtlety here: the join
    constrains the rows *before* the LIMIT, so a scoped search is exact for the
    same reason an unscoped one is.
    """
    match = escape_query(question)
    if not match or k <= 0:
        return []
    sql = """
        SELECT f.rowid AS chunk_id, bm25(chunks_fts) AS score
          FROM chunks_fts f
          JOIN chunks c ON c.id = f.rowid
         WHERE chunks_fts MATCH ?
    """
    params: list[object] = [match]
    if document_id is not None:
        sql += " AND c.document_id = ?"
        params.append(document_id)
    # ASCENDING, and this is not a typo waiting to be "fixed": bm25() returns a
    # negated score, so the best match is the most negative number. `DESC`
    # would return real matches that happen to be the worst ones -- a results
    # list that looks plausible and is upside down.
    sql += " ORDER BY score LIMIT ?"
    params.append(k)
    return [(int(row["chunk_id"]), -float(row["score"])) for row in conn.execute(sql, params)]


__all__ = ["Hit", "escape_query", "keyword_search"]
