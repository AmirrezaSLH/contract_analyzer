"""The document catalogue: what has been ingested, and how to drop it.

`db.py` opens the database, `ingest/` fills it, `retrieval/` ranks what is in
it. This module is the fourth thing a surface needs and none of those provide:
the queries that let a client **bind a session to one contract** before any
question is asked -- list what is stored, look one up, show its outline, remove
it. Nothing here ranks anything, which is why it is not in `retrieval/`, and
nothing here opens a connection, which is why it is not in `db.py`.

Two notes on the delete:

**One statement is enough, but it is verified rather than assumed.** `chunks`
cascades from `documents`, and the `chunks_ad` trigger takes the FTS and vec
rows with it. SQLite only *promises* that a trigger fires for a direct delete
-- for rows removed by a foreign-key action the manual makes it depend on
`recursive_triggers`, which is off by default. It does fire on the versions
this project runs against, and `tests/test_documents.py` asserts the outcome
rather than trusting the mechanism.

**The row and its bytes go together.** `documents.path` is relative to the
project root, so a deleted document takes its raw file with it unless the
caller says otherwise. A path that has wandered outside the project root is
left alone: this function deletes what it ingested, not what it can reach.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .config import PROJECT_ROOT
from .logger import get_logger

log = get_logger(__name__)

#: Everything `documents` holds, plus the chunk count a surface always wants.
_SELECT_DOCUMENT = """
SELECT d.id AS document_id, d.path, d.filename, d.content_hash, d.page_count,
       d.producer, d.has_outline, d.spine_source, d.ingested_at,
       (SELECT count(*) FROM chunks c WHERE c.document_id = d.id) AS chunks
  FROM documents d
"""


@dataclass(frozen=True, kw_only=True)
class Document:
    """One ingested file: its identity, its provenance, and how big it is."""

    document_id: int
    filename: str
    #: Relative to the project root, so the database stays portable.
    path: str
    content_hash: str
    page_count: int | None = None
    producer: str | None = None
    has_outline: bool = False
    #: `outline`, `headings` or `none` -- whether the breadcrumbs were read
    #: from the PDF or inferred. A reviewer checking a citation deserves it.
    spine_source: str = "none"
    ingested_at: str = ""
    chunks: int = 0


@dataclass(frozen=True, kw_only=True)
class Section:
    """One section of a contract, as the outline shows it."""

    #: The breadcrumb, outermost first: `["6. Identity", "6.6 Passwords"]`.
    path: list[str] = field(default_factory=list)
    #: The printed page or page range the section spans: `9` or `9-10`.
    page_display: str = ""
    chunks: int = 0

    @property
    def title(self) -> str:
        """The leaf, which is what a picker lists."""
        return self.path[-1] if self.path else ""

    @property
    def depth(self) -> int:
        return len(self.path)


def list_documents(conn: sqlite3.Connection, *, limit: int | None = None) -> list[Document]:
    """Every stored document, newest first.

    Newest first because the client that just uploaded one wants to find it,
    and `ingested_at` has one-second resolution -- `id` breaks the tie, so two
    uploads in the same second still come back in the order they happened.
    """
    sql = f"{_SELECT_DOCUMENT} ORDER BY d.ingested_at DESC, d.id DESC"
    params: tuple[int, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (int(limit),)
    return [_document(row) for row in conn.execute(sql, params)]


def get_document(conn: sqlite3.Connection, document_id: int) -> Document | None:
    """One document, or None. None rather than an exception: every caller here
    turns "no such id" into its own answer -- a 404, a skip, a prompt."""
    row = conn.execute(f"{_SELECT_DOCUMENT} WHERE d.id = ?", (int(document_id),)).fetchone()
    return _document(row) if row is not None else None


def document_sections(conn: sqlite3.Connection, document_id: int) -> list[Section]:
    """The document's outline, in document order, one entry per section.

    Built from the chunks rather than from the parser, because the chunks are
    what retrieval can actually reach: a section that produced no chunk is not
    in the index, and offering it in a picker would be a promise nothing can
    keep. Consecutive chunks sharing a breadcrumb are one entry, so a section
    split across four chunks is listed once with the page range of all four.
    """
    rows = conn.execute(
        "SELECT section_path, page_label, page_label_end FROM chunks "
        "WHERE document_id = ? ORDER BY ordinal",
        (int(document_id),),
    ).fetchall()

    sections: list[Section] = []
    first_label = last_label = ""
    current: str | None = None
    count = 0

    def flush() -> None:
        if current is None:
            return
        sections.append(
            Section(
                path=json.loads(current) if current else [],
                page_display=_page_display(first_label, last_label),
                chunks=count,
            )
        )

    for row in rows:
        key = row["section_path"] or "[]"
        if key != current:
            flush()
            current, count = key, 0
            first_label = row["page_label"] or ""
        count += 1
        last_label = row["page_label_end"] or row["page_label"] or last_label
    flush()
    return sections


def delete_document(
    conn: sqlite3.Connection, document_id: int, *, remove_file: bool = True
) -> bool:
    """Remove one document, its chunks, its vectors and its FTS rows.

    Returns False when there was no such document, so a caller can answer 404
    without a second query. The raw file goes too unless `remove_file=False`
    (the CLI's `--keep-file`, and any caller that did not put the file there).
    """
    document = get_document(conn, document_id)
    if document is None:
        return False
    with conn:
        conn.execute("DELETE FROM documents WHERE id = ?", (int(document_id),))
    if remove_file:
        _remove_file(document.path)
    log.info(
        "document.deleted",
        extra={"document_id": document.document_id, "chunks": document.chunks},
    )
    return True


def _document(row: sqlite3.Row) -> Document:
    return Document(
        document_id=row["document_id"],
        filename=row["filename"],
        path=row["path"] or "",
        content_hash=row["content_hash"],
        page_count=row["page_count"],
        producer=row["producer"],
        has_outline=bool(row["has_outline"]),
        spine_source=row["spine_source"] or "none",
        ingested_at=row["ingested_at"] or "",
        chunks=row["chunks"],
    )


def _page_display(first: str, last: str) -> str:
    """`9`, or `9-10` when the section crosses a page break. Matches
    `RetrievedChunk.page_display`, so a section and a citation print alike."""
    if last and last != first:
        return f"{first}-{last}"
    return first


def _remove_file(relative: str) -> None:
    """Delete the ingested file, if it is where the row says and inside the
    project. `missing_ok`: a file removed by hand is not an error here."""
    if not relative:
        return
    path = Path(relative)
    path = path if path.is_absolute() else PROJECT_ROOT / path
    try:
        path.resolve().relative_to(PROJECT_ROOT)
    except ValueError:
        log.info("document.file_kept", extra={"path": str(path)})
        return
    path.unlink(missing_ok=True)


__all__ = [
    "Document",
    "Section",
    "delete_document",
    "document_sections",
    "get_document",
    "list_documents",
]
