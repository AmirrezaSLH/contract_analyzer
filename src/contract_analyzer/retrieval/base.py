"""What a retriever returns, and the one query that builds it.

Both retrievers rank `chunks.id`s and nothing more: KNN hands back ids and
distances, BM25 hands back ids and scores. Everything a caller actually wants
-- the text, the page, the breadcrumb, the file it came from -- is read once,
here, after the ranking is settled. That is why fusion is cheap: RRF operates
on two short lists of integers, and exactly `top_k` rows are ever hydrated.

Three details in this file are load-bearing:

**The ranking survives hydration.** `WHERE id IN (...)` returns rows in
whatever order SQLite likes, which is not the order they were ranked in.
`hydrate` re-reads its input dict -- insertion-ordered -- so the list it
returns is the ranking. A chunk deleted between the search and the fetch drops
out rather than raising.

**A citation must be checkable.** A filename and a page number send a reviewer
hunting; the section is what lets them confirm the quote in seconds. So
`citation_title` names the section, and `page_display` prints `9-10` when the
clause crosses a page break rather than silently citing its first page.

**A table's text is not its grid.** `payload` is the bare markdown grid, which
is right for rendering and wrong for a model: a requirement matrix's cells say
"Password rotation" and never "6.6 Password Management Standard". The chunker
puts the breadcrumb in front of a table's `content` for exactly that reason,
and `text_for_model` keeps it there.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final


class _AllDocuments:
    """The corpus-wide scope, spelled out."""

    def __repr__(self) -> str:  # what a log line and a repr show
        return "ALL_DOCUMENTS"


#: Passed as `document_id=` to search every contract in the database. It exists
#: so that "search everything" is something a caller says on purpose: with a
#: default of `None`, a Phase B call site that forgot the argument would
#: quietly answer a question about one contract with another contract's text,
#: and the answer would look entirely reasonable.
ALL_DOCUMENTS: Final = _AllDocuments()

DocumentScope = int | _AllDocuments


@dataclass(frozen=True, kw_only=True)
class RetrievedChunk:
    """One chunk, with where it came from and how it scored.

    A superset of a `chunks` row: `filename` and `spine_source` come from the
    joined `documents` row, so a surface can render a citation -- and mark a
    section that was inferred rather than read from an outline -- without a
    second query.
    """

    chunk_id: int
    document_id: int
    ordinal: int
    content: str
    filename: str
    #: The document's path, relative to the project root.
    path: str = ""
    page: int | None = None
    page_label: str = ""
    page_end: int | None = None
    page_label_end: str = ""
    section: str = ""
    section_path: list[str] = field(default_factory=list)
    element_type: str = "paragraph"
    bbox: list[float] | None = None
    asset_path: str | None = None
    payload: str | None = None
    token_count: int = 0
    #: `outline`, `headings` or `none` -- whether the breadcrumb was read from
    #: the PDF or inferred. A reviewer checking a citation deserves to know.
    spine_source: str = "none"
    #: Higher is better, whatever produced it: RRF score in hybrid mode,
    #: cosine similarity in vector mode, negated BM25 in keyword mode.
    score: float = 0.0
    #: Cosine similarity, when this chunk came back from the vector side.
    similarity: float | None = None
    #: Which retriever ranked it where, 1-based: `{"vector": 3, "keyword": 1}`.
    #: What makes a fused result explainable instead of a number.
    ranks: dict[str, int] = field(default_factory=dict)

    @property
    def breadcrumb(self) -> str:
        """The full section path, `A > B > C`."""
        return " > ".join(self.section_path)

    @property
    def page_display(self) -> str:
        """The printed page or page range a citation should show."""
        if self.page_label_end and self.page_label_end != self.page_label:
            return f"{self.page_label}-{self.page_label_end}"
        return self.page_label

    @property
    def citation_title(self) -> str:
        """`Sample Contract.pdf — 6.6 Password Management Standard (p.9-10)`.

        The *leaf* section, not the whole breadcrumb: a deep path is longer
        than the line it has to fit on, and the leaf plus the page is what a
        reviewer needs to find the clause. The full path is still on the object
        as `.breadcrumb` for a surface that has room for it. Each part is
        dropped rather than faked when it is missing, so a chunk from an
        outline-less page still gets a usable title.
        """
        leaf = self.section_path[-1] if self.section_path else self.section
        title = f"{self.filename} — {leaf}" if leaf else self.filename
        return f"{title} (p.{self.page_display})" if self.page_display else title

    def text_for_model(self) -> str:
        """What an answer model should be shown.

        For a table, the markdown grid *with its breadcrumb in front*: the grid
        is the readable form, and the breadcrumb is the only thing that says
        which section's requirements these rows are.
        """
        if self.element_type == "table" and self.payload:
            return f"{self.breadcrumb}\n{self.payload}" if self.breadcrumb else self.payload
        return self.content


@dataclass(frozen=True, kw_only=True)
class RetrievalResult:
    """A ranked answer to one question, plus what it took to produce it."""

    question: str
    mode: str
    #: The contract the search was scoped to, or None for the whole corpus.
    document_id: int | None
    chunks: list[RetrievedChunk]
    candidates: int
    top_k: int
    #: Wall time per stage, milliseconds -- what the KPI page reports.
    timings: dict[str, float] = field(default_factory=dict)

    def __iter__(self):
        return iter(self.chunks)

    def __len__(self) -> int:
        return len(self.chunks)

    def __bool__(self) -> bool:
        return bool(self.chunks)


def similarity_from_distance(distance: float) -> float:
    """L2 distance between unit vectors as a cosine similarity in [0, 1].

    `vec0` ranks by L2, and every embedder here normalises, so
    ||a-b||² = 2 - 2·cos and the two orderings are identical. This converts the
    number rather than inventing one: a reader who sees "0.82" assumes cosine.
    Clamped, because floating point puts an exact match a hair below zero
    distance and a hair above 1.0.
    """
    similarity = 1.0 - (distance * distance) / 2.0
    return min(1.0, max(0.0, similarity))


#: Everything a citation needs, in one join. `documents` is joined for the
#: filename anyway, so `spine_source` rides along for free.
SELECT_CHUNK = """
SELECT c.id AS chunk_id, c.document_id, c.ordinal, c.content, c.page, c.page_label,
       c.page_end, c.page_label_end, c.section, c.section_path, c.element_type,
       c.bbox, c.asset_path, c.payload, c.token_count,
       d.filename, d.path, d.spine_source
  FROM chunks c
  JOIN documents d ON d.id = c.document_id
"""


def chunk_from_row(
    row: sqlite3.Row,
    *,
    score: float = 0.0,
    similarity: float | None = None,
    ranks: Mapping[str, int] | None = None,
) -> RetrievedChunk:
    """A `chunks`+`documents` row as a `RetrievedChunk`. JSON columns decoded."""
    return RetrievedChunk(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        ordinal=row["ordinal"],
        content=row["content"],
        filename=row["filename"],
        path=row["path"] or "",
        page=row["page"],
        page_label=row["page_label"] or "",
        page_end=row["page_end"],
        page_label_end=row["page_label_end"] or "",
        section=row["section"] or "",
        section_path=_json_list(row["section_path"]),
        element_type=row["element_type"] or "paragraph",
        bbox=_json_list(row["bbox"]) or None,
        asset_path=row["asset_path"],
        payload=row["payload"],
        token_count=row["token_count"] or 0,
        spine_source=row["spine_source"] or "none",
        score=score,
        similarity=similarity,
        ranks=dict(ranks or {}),
    )


def hydrate(
    conn: sqlite3.Connection,
    scores: Mapping[int, float],
    *,
    similarities: Mapping[int, float] | None = None,
    ranks: Mapping[int, Mapping[str, int]] | None = None,
) -> list[RetrievedChunk]:
    """Fetch the ranked chunks, in the order `scores` lists them.

    `scores` is read as an ordered mapping -- it *is* the ranking. Ids that no
    longer resolve are skipped: a chunk can be deleted between the search and
    this fetch, and losing a row from a list of results is not worth an
    exception.
    """
    ids = list(scores)
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = {
        row["chunk_id"]: row
        for row in conn.execute(f"{SELECT_CHUNK} WHERE c.id IN ({placeholders})", ids)
    }
    return [
        chunk_from_row(
            rows[chunk_id],
            score=scores[chunk_id],
            similarity=(similarities or {}).get(chunk_id),
            ranks=(ranks or {}).get(chunk_id),
        )
        for chunk_id in ids
        if chunk_id in rows
    ]


def _json_list(value: str | None) -> list:
    """A JSON column as a list. A malformed one is empty, not fatal."""
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    return list(decoded) if isinstance(decoded, Sequence) and not isinstance(decoded, str) else []


__all__ = [
    "ALL_DOCUMENTS",
    "SELECT_CHUNK",
    "DocumentScope",
    "RetrievalResult",
    "RetrievedChunk",
    "chunk_from_row",
    "hydrate",
    "similarity_from_distance",
]
