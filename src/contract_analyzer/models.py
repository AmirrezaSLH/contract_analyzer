"""The records that travel between pipeline stages.

`Element` (in `parse/`) is what a *document* is made of; `Chunk` is what the
*index* is made of. The two are deliberately different: an element is one thing
on a page, a chunk is one unit of retrieval, and the chunker's whole job is to
turn a stream of the first into a list of the second.

`Chunk`'s fields map 1:1 onto the `chunks` columns in `schema.sql`, minus the
two the pipeline fills in when it writes the row (`document_id`,
`embedding_model`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .parse.elements import BBox, ElementType


@dataclass(frozen=True, kw_only=True)
class Chunk:
    """One retrieval unit: the text that gets embedded, indexed and cited."""

    #: Position in the document. With a document id it identifies a chunk
    #: stably across re-ingestion, which is what makes ingestion idempotent.
    ordinal: int
    #: What the embedder sees and what FTS5 indexes -- there is only one text,
    #: so a chunk cannot be retrieved by words it does not contain.
    content: str
    #: 0-based physical page: what to open the file at.
    page: int | None = None
    #: The *printed* page ("37", "xxi") -- the number the reader's eye finds.
    page_label: str = ""
    section: str = ""
    section_path: list[str] = field(default_factory=list)
    #: What the chunk is made of. Retrieval and rendering both branch on it.
    element_type: ElementType = "paragraph"
    #: The first element's box, for highlighting. It *anchors* the citation, it
    #: does not delimit it: a union across a packed chunk spans half a page and
    #: highlights nothing useful.
    bbox: BBox | None = None
    #: Figure image, relative to the project root so the database stays
    #: portable and no home directory leaks into a citation.
    asset_path: str | None = None
    #: The structure `content` flattens: a table's markdown grid, or a figure's
    #: panel list. Kept apart so the UI can render it rather than re-parse it.
    payload: str | None = None
    token_count: int = 0

    @property
    def breadcrumb(self) -> str:
        return " > ".join(self.section_path)


__all__ = ["Chunk"]
