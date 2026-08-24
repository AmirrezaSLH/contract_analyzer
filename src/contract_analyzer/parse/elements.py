"""The typed units a document is parsed into.

`PHASE_2.md` treated a PDF as one text string plus a page map, which discards
every structure that is not a run of prose. Parsing instead produces an ordered
list of *elements*, so the chunker packs meaning-bearing units -- a table stays
whole, a figure keeps its caption -- rather than slicing a wall of text.

Each element carries everything a citation needs: the physical page (to open
the file at the right place) and the printed label (to show the reader the page
number their eye will find). Those two differ by 13 pages in one of our corpus
files and 20 in the other, which is exactly the kind of error nobody notices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ElementType = Literal[
    "heading",
    "paragraph",
    "table",
    "figure",
    "caption",
    "equation",
    "furniture",
]

#: How much structure a table extraction actually recovered. Recorded per table
#: so the ingest report can say "12 ruled, 6 recovered, 5 fallback" -- an honest
#: number is worth more than a mangled grid presented as data.
TableQuality = Literal["ruled", "recovered", "text-fallback"]

BBox = tuple[float, float, float, float]


@dataclass(kw_only=True)
class Element:
    """One meaning-bearing unit of a document, in reading order.

    `section`, `section_path` and `order` are filled in by a later pass
    (`outline.assign_sections`), so extraction can emit elements page by page
    without knowing where they sit in the document as a whole.
    """

    type: ElementType
    text: str
    page_index: int  # 0-based physical page -- for opening the file
    page_label: str  # printed page ("37", "xxi") -- for display
    bbox: BBox
    section: str = ""
    section_path: list[str] = field(default_factory=list)
    order: int = -1

    def __post_init__(self) -> None:
        # `text` is what gets embedded and shown. An element with nothing to
        # show is a bug in extraction, not a valid empty case -- catching it
        # here beats discovering it as an empty citation at answer time.
        if not self.text.strip():
            raise ValueError(f"{self.type} element on page {self.page_index} has empty text")

    @property
    def breadcrumb(self) -> str:
        """The section path as one display string."""
        return " > ".join(self.section_path)


@dataclass(kw_only=True)
class TableElement(Element):
    """A table, never split across chunks: half a table is worse than useless."""

    type: ElementType = "table"
    markdown: str = ""
    rows: list[list[str]] = field(default_factory=list)
    caption: str = ""
    #: Where the caption sits on the page. The caption's own text block is
    #: removed from the prose stream using this, since `text` already contains
    #: it -- otherwise every caption is indexed twice.
    caption_bbox: BBox | None = None
    quality: TableQuality = "text-fallback"

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        return max((len(r) for r in self.rows), default=0)


@dataclass(kw_only=True)
class FigureElement(Element):
    """A figure: one or more raster assets on disk, plus the caption that
    describes them.

    Multi-panel figures embed one image per panel -- our corpus has 74 images
    against 53 captions -- so `asset_paths` is a list, not a single path.
    """

    type: ElementType = "figure"
    asset_paths: list[Path] = field(default_factory=list)
    caption: str = ""
    #: See `TableElement.caption_bbox`.
    caption_bbox: BBox | None = None
    width: int = 0
    height: int = 0
    #: Optional VLM-generated summary, populated only under --describe-figures.
    description: str | None = None

    @property
    def n_panels(self) -> int:
        return len(self.asset_paths)
