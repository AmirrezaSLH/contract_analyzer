"""The parser: a PDF file becomes an ordered list of typed elements.

This is the module the rest of the pipeline calls. It runs the passes in the
order their dependencies require:

1. profile the document (body font size, vocabulary, running furniture);
2. read the outline and page labels (and, when there is no outline,
   synthesize the section spine from the document's own numbering after
   step 5);
3. per page, claim table and figure regions, then extract the text that is left;
4. pin outline entries to the headings actually rendered, so a section starts
   where its title does and not at the top of the page;
5. find the document's clause enumerators and corroborate them by sequence,
   split any element that holds two clauses, then rejoin the lines a PDF
   hands out as separate blocks into paragraphs -- including across a page
   break, but never across a corroborated clause boundary;
6. assign sections and reading order to every element.

Step 3's ordering is the one that matters most: text inside a table or figure
region is removed from the prose stream, so nothing is indexed twice.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import pymupdf

from .blocks import (
    DocumentProfile,
    extract_text_elements,
    join_lines,
    profile_document,
)
from .elements import Element, FigureElement, TableElement
from .enumerators import EnumeratorLattice
from .outline import (
    Section,
    assign_sections,
    build_spine,
    locate_headings,
    page_labels,
    synthesize_spine,
)

#: Three or more spaced dots: a table-of-contents row. Those are full width and
#: flush left like body text, so the geometry alone would weld the whole
#: contents page into one element.
_DOT_LEADER = re.compile(r"\.\s?\.\s?\.")

#: No real paragraph is this long. The cap bounds the damage when the shape
#: test is fooled: a reference list is full width with a hanging indent, and
#: without it Sparks's bibliography merges into a single 16,000-token element.
_MAX_PARAGRAPH_CHARS = 4000

#: Slack around the measured paragraph indent when testing whether a line
#: sits in the text column. The indent itself is measured per document
#: (`DocumentProfile.paragraph_indent`); a document that does not indent gets
#: no slack at all, so its indented blocks are not mistaken for prose.
_INDENT_SLACK = 6.0

#: Vertical distance between two lines of the same paragraph, in points. The
#: widest measured on this corpus is 32pt (double-spaced 12pt text); anything
#: much beyond that means a table or figure was lifted out from between them,
#: and the two lines are not neighbours at all.
_MAX_LINE_GAP = 36.0


@dataclass
class ParsedDocument:
    """Everything parsing recovered from one file."""

    path: Path
    content_hash: str
    page_count: int
    producer: str
    has_outline: bool
    #: Content elements in reading order -- what the chunker consumes.
    elements: list[Element] = field(default_factory=list)
    #: Page numbers and running headers, dropped from the content stream but
    #: kept so the ingest report can show what was discarded. Silent deletion
    #: is far harder to debug than a list you can look at.
    furniture: list[Element] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    #: Where the section structure came from: the PDF's own ``/Outlines``,
    #: the document's headings and clause numbering (`synthesize_spine`), or
    #: nowhere -- in which case every `section_path` is honestly empty.
    spine_source: str = "none"
    profile: DocumentProfile | None = None

    def of_type(self, kind: str) -> list[Element]:
        return [e for e in self.elements if e.type == kind]

    @property
    def tables(self) -> list[TableElement]:
        return [e for e in self.elements if isinstance(e, TableElement)]

    @property
    def figures(self) -> list[FigureElement]:
        return [e for e in self.elements if isinstance(e, FigureElement)]

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for element in self.elements:
            counts[element.type] = counts.get(element.type, 0) + 1
        counts["furniture(dropped)"] = len(self.furniture)
        return counts


def file_hash(path: Path) -> str:
    """SHA-256 of the file's bytes; re-ingesting an unchanged file is a no-op."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_pdf(
    path: Path | str,
    *,
    assets_dir: Path | None = None,
    extract_figures: bool = True,
    extract_tables: bool = True,
) -> ParsedDocument:
    """Parse one PDF into elements.

    `assets_dir` is where figure images are written; figures are skipped if it
    is None. The two `extract_*` switches exist so the parse report -- and the
    tests -- can isolate one stage at a time.
    """
    path = Path(path)
    assets_dir = Path(assets_dir) if assets_dir is not None else None
    doc = pymupdf.open(path)
    try:
        profile = profile_document(doc)
        spine = build_spine(doc)
        labels = page_labels(doc)

        parsed = ParsedDocument(
            path=path,
            content_hash=file_hash(path),
            page_count=doc.page_count,
            producer=str(doc.metadata.get("producer") or ""),
            has_outline=bool(spine),
            sections=spine,
            spine_source="outline" if spine else "none",
            profile=profile,
        )

        from .figures import FigureExtractor
        from .tables import extract_tables_from_page

        figure_extractor = (
            FigureExtractor(
                doc=doc, assets_dir=assets_dir, document_id=path.stem, profile=profile
            )
            if extract_figures and assets_dir is not None
            else None
        )

        content: list[Element] = []
        furniture: list[Element] = []

        for page in doc:
            label = labels[page.number]
            claimed: list[pymupdf.Rect] = []

            tables = (
                extract_tables_from_page(page, label, profile) if extract_tables else []
            )
            claimed.extend(pymupdf.Rect(t.bbox) for t in tables)
            claimed.extend(_caption_rects(tables))

            figures = (
                figure_extractor.page_figures(page, label, claimed)
                if figure_extractor is not None
                else []
            )
            claimed.extend(pymupdf.Rect(f.bbox) for f in figures)
            claimed.extend(_caption_rects(figures))

            text = extract_text_elements(page, label, profile, exclude=claimed)

            # Pin this page's outline entries to their rendered headings before
            # sections are assigned, so text above a heading stays in the
            # previous section. Every text element is offered, not just those
            # already classified as headings: a deep subsection set bold at the
            # body size fails the size test but still matches the outline.
            absorbed = locate_headings(spine, page.number, text)
            if absorbed:
                # Continuation halves of a wrapped heading, now folded into it.
                text = [e for e in text if e not in absorbed]

            page_elements = [e for e in text if e.type != "furniture"]
            furniture.extend(e for e in text if e.type == "furniture")

            # Tables and figures are placed by vertical position among the
            # page's prose rather than appended, so reading order survives.
            page_elements.extend(tables)
            page_elements.extend(figures)
            page_elements.sort(key=lambda e: (round(e.bbox[1] / 3), e.bbox[0]))
            content.extend(page_elements)

        lattice = EnumeratorLattice.from_elements(content)
        content = split_welded(content, lattice)
        content = join_wrapped_lines(content, profile, lattice=lattice)
        if not spine:
            spine = synthesize_spine(content, lattice)
            parsed.sections = spine
            parsed.spine_source = "headings" if spine else "none"
        assign_sections(content, spine)
        assign_sections(furniture, spine)
        _label_uncaptioned_figures(content)

        parsed.elements = content
        parsed.furniture = furniture
        return parsed
    finally:
        doc.close()


def split_welded(elements: list[Element], lattice: EnumeratorLattice) -> list[Element]:
    """Split a paragraph that holds more than one clause.

    A single extracted block occasionally contains two numbered clauses when
    the typesetter left no vertical gap between them. Each corroborated
    sectional enumerator inside the text starts a new element. The fragments
    inherit the host's horizontal extent and are given a vertical position in
    proportion to where their text begins, which keeps them in reading order
    and lets a section spine be pinned to them.
    """
    out: list[Element] = []
    for element in elements:
        if element.type != "paragraph":
            out.append(element)
            continue
        cuts = lattice.positions(element.text)
        if not cuts:
            out.append(element)
            continue
        x0, y0, x1, y1 = element.bbox
        height = y1 - y0
        bounds = [0, *cuts, len(element.text)]
        for start, end in zip(bounds, bounds[1:], strict=False):
            text = element.text[start:end].strip()
            if not text:
                continue
            top = y0 + height * start / len(element.text)
            bottom = y0 + height * end / len(element.text)
            out.append(replace(element, text=text, bbox=(x0, top, x1, bottom)))
    return out


def join_wrapped_lines(
    elements: list[Element],
    profile: DocumentProfile,
    lattice: EnumeratorLattice | None = None,
) -> list[Element]:
    """Rebuild paragraphs from the lines a PDF hands out as separate blocks.

    PyMuPDF groups lines into a block only when their spacing is tight, so a
    double-spaced document arrives one block per *line*: 3,079 "paragraphs" in
    Sparks with a p25-p75 of 20-22 tokens, which is one 459pt line of 12pt
    text. That is not a unit anything downstream can use -- 210 of those
    elements end mid-word in a hyphen the block-level de-hyphenation never sees,
    and a lone line has no subject to embed.

    The test is geometric, and it subsumes the page-break case this pass used
    to handle on its own: a line that runs out to the right margin was wrapped,
    and the line after it continues that paragraph if it starts flush at the
    left margin. An indented line (18pt in both corpus files) begins a new
    paragraph instead. A heading, equation, table or figure between two lines
    ends the run, since the merge only ever considers adjacent paragraphs.

    Geometry is not enough for a document that does not indent: a new clause
    and a wrapped line then both start flush at the margin. The `lattice`
    supplies the structural evidence -- a line that opens a corroborated
    enumerator begins a clause, whatever the line before it looked like.

    The merged element keeps the *first* line's page index and label, because
    that is where a reader following the citation should look.
    """
    merged: list[Element] = []

    for element in elements:
        prev = merged[-1] if merged else None
        if prev is not None and _continues(prev, element, profile, lattice):
            # join_lines applies the document's own vocabulary to the hyphen at
            # the break, exactly as it does for lines within one block.
            prev.text = join_lines([prev.text, element.text], profile)
            if element.page_index == prev.page_index:
                prev.bbox = (
                    min(prev.bbox[0], element.bbox[0]),
                    prev.bbox[1],
                    max(prev.bbox[2], element.bbox[2]),
                    element.bbox[3],
                )
            continue
        merged.append(element)

    return merged


def _continues(
    prev: Element,
    element: Element,
    profile: DocumentProfile,
    lattice: EnumeratorLattice | None = None,
) -> bool:
    """Whether `element` is the next line of the paragraph `prev` started."""
    if prev.type != "paragraph" or element.type != "paragraph":
        return False
    if lattice is not None and lattice.opens(element) is not None:
        return False  # it opens a clause of its own, whatever the geometry says
    if not profile.reaches_text_width(prev.bbox[2]):
        return False  # the previous line stopped short: it ended its paragraph
    indent = profile.paragraph_indent + _INDENT_SLACK if profile.paragraph_indent else 2.0
    if not profile.starts_at_text_left(prev.bbox[0], tolerance=indent):
        # The line being continued must itself sit in the text column: at the
        # margin, or one indent in if this document indents paragraphs. The
        # right-hand half of a display equation also reaches the right margin
        # -- starting at 336pt, it is not prose.
        return False
    if not profile.starts_at_text_left(element.bbox[0]):
        return False  # indented, so it opens a paragraph rather than continuing one
    if _DOT_LEADER.search(prev.text) or _DOT_LEADER.search(element.text):
        return False  # a contents page, whose rows are full width and unrelated
    if len(prev.text) + len(element.text) > _MAX_PARAGRAPH_CHARS:
        return False
    if element.page_index != prev.page_index:
        return True  # a page break is just another line break to this rule
    # A gap far wider than the leading means something was lifted out from
    # between the two lines -- a claimed table or figure region.
    return element.bbox[1] - prev.bbox[3] <= _MAX_LINE_GAP


def _caption_rects(elements: list[Element]) -> list[pymupdf.Rect]:
    """The caption boxes already absorbed into a table or figure element.

    Their text is part of that element's own `text`, so leaving the block in
    the prose stream would index every caption twice -- once attached to the
    thing it describes and once as a dangling fragment.
    """
    return [
        pymupdf.Rect(e.caption_bbox)
        for e in elements
        if getattr(e, "caption_bbox", None) is not None
    ]


def _label_uncaptioned_figures(elements: list[Element]) -> None:
    """Give a figure with no `\\caption` something worth indexing.

    Appendix plots are frequently set without a caption -- 6 in one corpus file
    and 16 in the other, all of them real content. The section they sit in is
    the best available description, and it is how a reader refers to them
    anyway ("the plots in Alternative Parameters"). Runs after section
    assignment, which is why it is a separate pass.
    """
    for element in elements:
        if isinstance(element, FigureElement) and not element.caption:
            where = element.section or "the document"
            element.text = f"Figure in {where} (page {element.page_label})"


__all__ = ["ParsedDocument", "file_hash", "join_wrapped_lines", "parse_pdf", "split_welded"]
