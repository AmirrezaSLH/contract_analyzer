"""PDF parsing: a document becomes an ordered list of typed elements.

Replaces the `loaders.py` of `PHASE_2.md`, which returned one text string plus
a page map. See `work_info/PHASE_2_PDF_PARSING.md` for the measurements this
design is built on.
"""

from .elements import BBox, Element, ElementType, FigureElement, TableElement, TableQuality
from .outline import Section, assign_sections, build_spine, page_labels
from .pdf import ParsedDocument, parse_pdf

__all__ = [
    "BBox",
    "Element",
    "ElementType",
    "FigureElement",
    "ParsedDocument",
    "Section",
    "TableElement",
    "TableQuality",
    "assign_sections",
    "build_spine",
    "page_labels",
    "parse_pdf",
]
