"""Section structure and page labels, read from the PDF rather than guessed.

Two things a well-produced PDF stores explicitly, which `PHASE_2.md` was
planning to reconstruct by heuristic:

* ``/Outlines`` -- the author's own table of contents: title, nesting level and
  destination page. Both corpus files carry a complete one (62 entries at depth
  4, 108 at depth 3), so heading regexes and dot-leader parsing are not needed.
* ``/PageLabels`` -- the *printed* page number, which is not the physical page
  index whenever there is roman-numbered front matter. The offset is 13 pages
  in one corpus file and 20 in the other.

The fallback for a PDF carrying neither (anything a user uploads later) is font
-size clustering, in `blocks.py`; this module is the primary path.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

import pymupdf

from .elements import Element

#: Ligatures pdfTeX paints as single glyphs, plus the dashes and quotes that
#: differ between an outline title and the same words rendered on the page.
#: Quote characters are dropped rather than folded: LaTeX writes ``...'' into
#: the outline and renders curly quotes on the page, so no single mapping makes
#: the two agree.
_GLYPH_FOLD = str.maketrans(
    {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl",
     "–": "-", "—": "-",
     "‘": "", "’": "", "“": "", "”": "", "`": "", "'": "", '"': ""}
)

_WS = re.compile(r"\s+")
#: A leading section number, as it appears in a rendered heading but often not
#: in the outline title (or vice versa): "2.1", "A.1", "Chapter 4". The
#: `[A-Z]\.?` before the digits is what matches appendix numbering -- without
#: it "A.1 Projection Into Future" keeps its number and never matches the
#: outline's bare "Projection Into Future".
_LEADING_NUMBER = re.compile(
    r"^(?:chapter|section|appendix)?\s*(?:[A-Z]\.?)?\d+(?:\.\d+)*\.?\s+", re.I
)


def normalize_title(text: str) -> str:
    """Fold a title to a form that compares equal across outline and page.

    Used to match an outline entry against the heading block actually rendered
    on the page, which is what lets a section start at a y-position rather than
    at the top of a page.
    """
    text = unicodedata.normalize("NFKC", text).translate(_GLYPH_FOLD)
    return _WS.sub(" ", text).strip().casefold()


def strip_leading_number(title: str) -> str:
    """`"2.1 Building Airtightness"` -> `"Building Airtightness"`."""
    return _LEADING_NUMBER.sub("", title).strip()


@dataclass
class Section:
    """One outline entry, resolved to a position in the document."""

    level: int  # 1-based nesting depth, as PyMuPDF reports it
    title: str
    page_index: int  # 0-based physical page the section starts on
    #: Vertical position of the heading on that page, once a rendered heading
    #: block has been matched to this entry. Until then the section is taken to
    #: start at the very top of its page.
    start_y: float | None = None
    #: This entry's title preceded by its ancestors', outermost first.
    path: list[str] = field(default_factory=list)

    @property
    def sort_key(self) -> tuple[int, float]:
        return (self.page_index, -1.0 if self.start_y is None else self.start_y)


def build_spine(doc: pymupdf.Document) -> list[Section]:
    """The outline as a flat, document-ordered list with ancestry resolved.

    PyMuPDF's `get_toc()` returns `[level, title, page]` with **1-based** pages;
    we store 0-based throughout and convert exactly here.
    """
    spine: list[Section] = []
    ancestry: list[str] = []  # title at each depth, index 0 == level 1

    for level, title, page in doc.get_toc():
        title = _WS.sub(" ", title).strip()
        if not title:
            continue
        # A destination page of 0 or -1 means the outline entry has no usable
        # target; anchor it to the first page rather than dropping the section.
        page_index = max(page - 1, 0)

        del ancestry[level - 1 :]  # pop back to this entry's parent
        ancestry.append(title)
        spine.append(Section(level=level, title=title, page_index=page_index, path=list(ancestry)))

    return spine


def page_labels(doc: pymupdf.Document) -> list[str]:
    """The printed page label for every page, falling back to the 1-based index.

    A PDF with no `/PageLabels` returns empty strings, in which case the printed
    number and the physical index genuinely do coincide.
    """
    labels = []
    for i in range(doc.page_count):
        label = doc[i].get_label()
        labels.append(label if label else str(i + 1))
    return labels


def locate_headings(
    spine: list[Section], page_index: int, elements: list[Element]
) -> list[Element]:
    """Pin outline entries to the y-position of the heading rendered on a page.

    Without this, every element on a page where a section begins is attributed
    to that section -- including the elements *above* the heading, which belong
    to the previous one. Table 3.1 of the Salehi corpus file is the worked
    example: it sits at y=184 on a page where "3.2.3.2 Intervention Scenario"
    begins at y=555, and without a pinned position it is filed under a section
    that starts 370 points below it.

    Candidates are **all** the page's elements, not only those already
    classified as headings. A deep subsection is often set bold at the *body*
    font size, which the size-based rule in `blocks.classify` deliberately
    excludes so that inline bold runs are not mistaken for headings. The
    outline is the authority here: if a block's text is exactly an outline
    entry's title, it is that heading regardless of how it was set, and it is
    promoted to `heading` in place.

    Matching is exact on the normalized title, tried with and without a leading
    section number, since the outline and the printed heading do not agree on
    whether to include it. Exactness is what keeps a paragraph that merely
    *mentions* a section title from being promoted.

    Mutates both `spine` and the matched elements; unmatched entries keep
    `start_y = None` and so continue to start at the top of their page. Returns
    the continuation blocks folded into a wrapped heading, for the caller to
    drop from the element stream.
    """
    if not elements:
        return []

    candidates = [s for s in spine if s.page_index == page_index and s.start_y is None]
    if not candidates:
        return []

    usable = [e for e in elements if e.type not in {"table", "figure", "furniture"}]

    # key -> (element that starts the heading, element absorbed into it or None)
    by_title: dict[str, tuple[Element, Element | None]] = {}

    def offer(text: str, start: Element, absorbed: Element | None) -> None:
        by_title.setdefault(normalize_title(text), (start, absorbed))
        by_title.setdefault(normalize_title(strip_leading_number(text)), (start, absorbed))

    for index, element in enumerate(usable):
        offer(element.text, element, None)
        # A long heading wraps, and the wrap can fall across a block boundary:
        # "3.5.2 Benefits of ... their Interac-" / "tion". Neither half matches
        # the outline alone, so offer the pair as well.
        if index + 1 < len(usable):
            nxt = usable[index + 1]
            if nxt.page_index == element.page_index:
                offer(_stitch(element.text, nxt.text), element, nxt)

    absorbed: list[Element] = []
    for section in candidates:
        keys = (
            normalize_title(section.title),
            normalize_title(strip_leading_number(section.title)),
        )
        for key in keys:
            found = by_title.get(key)
            if found is None:
                continue
            start, continuation = found
            section.start_y = start.bbox[1]
            start.type = "heading"
            if continuation is not None:
                start.text = _stitch(start.text, continuation.text)
                absorbed.append(continuation)
            break

    return absorbed


def _stitch(first: str, second: str) -> str:
    """Join a heading's two halves, closing a hyphen left by the line wrap."""
    if first.endswith("-"):
        return f"{first[:-1]}{second}"
    return f"{first} {second}"


def assign_sections(elements: list[Element], spine: list[Section]) -> None:
    """Fill in `section`, `section_path` and `order` on every element.

    For each element, the enclosing section is the *last* outline entry that
    begins at or before it -- comparing by page, then by vertical position
    where `locate_headings` has pinned one. Elements before the first outline
    entry (a title page, say) simply get no section.

    Mutates in place; `elements` must already be in reading order.
    """
    ordered_spine = sorted(spine, key=lambda s: s.sort_key)

    cursor = 0
    for order, element in enumerate(elements):
        element.order = order
        here = (element.page_index, element.bbox[1])

        # Both lists are in document order, so the spine pointer only advances.
        while cursor < len(ordered_spine) and ordered_spine[cursor].sort_key <= here:
            cursor += 1

        if cursor == 0:
            element.section = ""
            element.section_path = []
        else:
            current = ordered_spine[cursor - 1]
            element.section = current.title
            element.section_path = list(current.path)
