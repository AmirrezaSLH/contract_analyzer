"""PDF parsing: a document becomes an ordered list of typed elements.

See `docs/parsing.md` for the design and the measurements it rests on.
"""

from .elements import BBox, Element, ElementType, FigureElement, TableElement, TableQuality
from .enumerators import Enumerator, EnumeratorLattice, match_enumerator
from .outline import Section, assign_sections, build_spine, page_labels, synthesize_spine
from .pdf import ParsedDocument, parse_pdf

__all__ = [
    "BBox",
    "Element",
    "ElementType",
    "Enumerator",
    "EnumeratorLattice",
    "FigureElement",
    "ParsedDocument",
    "Section",
    "TableElement",
    "TableQuality",
    "assign_sections",
    "build_spine",
    "match_enumerator",
    "page_labels",
    "parse_pdf",
    "synthesize_spine",
]
