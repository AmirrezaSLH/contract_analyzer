"""Turning positioned glyphs into classified text elements.

A PDF content stream records where to paint marks, not what they mean, so
everything here is inference over geometry and font metadata. The inferences
are kept explicit and measured rather than tuned by feel -- see
`work_info/PHASE_2_PDF_PARSING.md`.

Two document-wide facts have to be gathered before any page can be classified,
which is why `profile_document` runs first:

* the **modal body font size**, so "larger than the body" defines a heading
  without hard-coding 12.0pt;
* the document's own **vocabulary**, which is what resolves the hyphenation
  ambiguity. `build-` + `ing` should join; `single-` + `family` must not. No
  dictionary can tell those apart, but the document can: `single-family` occurs
  hyphenated elsewhere in the text, and `buildingg`... does not.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

from .elements import BBox, Element

#: PyMuPDF span flag bits.
_FLAG_ITALIC = 1 << 1
_FLAG_BOLD = 1 << 4

#: Fonts pdfTeX uses for mathematics. A block made mostly of these is a
#: displayed equation, and extracting it as prose produces the `E =tout ·Cout`
#: soup the profiling found -- better to mark it and keep it verbatim.
_MATH_FONT = re.compile(r"CM(MI|SY|EX)|MSAM|MSBM|STIX|Math", re.I)

#: A figure or table caption, as LaTeX numbers them: "Figure 2.1:", "Table A.3.".
#: The lookahead is load-bearing: without it the terminator `[.:]` happily
#: matches the decimal point in "Figure 3.1 conceptualizes ...", turning an
#: ordinary sentence of prose into a caption.
CAPTION_RE = re.compile(
    r"^\s*(Figure|Table)\s+(?:[A-Z]\.?)?\d+(?:\.\d+)*\s*[.:](?=\s|$)", re.I
)

#: A page number standing alone -- arabic or roman, either case.
_BARE_NUMBER = re.compile(r"^[ivxlcdm\d]+$", re.I)

_WS = re.compile(r"\s+")
_DIGITS = re.compile(r"\d+")

#: A heading must exceed the body size by more than rounding noise. Inline bold
#: runs (CMBX12 at the body's own 12.0pt) are deliberately excluded by this.
HEADING_SIZE_MARGIN = 0.5

#: Fraction of a page's height below which nothing is treated as footer
#: furniture. The corpus footers sit at y0 = 691 of 792 (0.872).
_FOOTER_BAND = 0.85
_HEADER_BAND = 0.06

#: A line repeated across at least this share of pages (and this many pages) is
#: running furniture rather than content.
_REPEAT_SHARE = 0.20
_REPEAT_MIN_PAGES = 3


def normalize_ws(text: str) -> str:
    """Collapse whitespace runs to single spaces.

    Justified LaTeX text arrives as `'The   degree   of'`: the extractor turns
    each stretched inter-word gap into several spaces. Every text value in this
    module passes through here.
    """
    return _WS.sub(" ", text).strip()


@dataclass
class DocumentProfile:
    """Document-wide measurements the per-page classifier needs."""

    body_size: float
    page_count: int
    #: The modal horizontal extent of the text column, in points. `body_right`
    #: is the one that discriminates a table from prose: body prose runs out to
    #: the right margin and a table's rows almost never do, which is what lets a
    #: table with no rules at all be separated from the prose around it.
    #:
    #: `body_left` is measured only over blocks that *do* reach `body_right`,
    #: because the naive modal left edge is not the text margin: LaTeX indents
    #: the first line of a paragraph and a reference list hangs its
    #: continuations, so counting every block gives 111pt for Sparks where the
    #: text margin is plainly 81pt -- the same 81pt as Salehi. Restricted to
    #: full-width lines, both files agree.
    body_left: float = 0.0
    body_right: float = 0.0
    #: Words seen hyphenated somewhere in the document ("single-family").
    hyphenated: Counter[str] = field(default_factory=Counter)
    #: Every word seen, unhyphenated ("building").
    words: Counter[str] = field(default_factory=Counter)
    #: Digit-masked texts that recur in the header/footer bands.
    furniture_patterns: set[str] = field(default_factory=set)

    def is_heading_size(self, size: float) -> bool:
        return size > self.body_size + HEADING_SIZE_MARGIN

    def reaches_text_width(self, x1: float, tolerance: float = 8.0) -> bool:
        """Whether a block runs out to the right margin, as prose does.

        Only the right edge is tested when telling a table from prose: an
        indented first line and a hanging indent both move the left edge, while
        a multi-line paragraph's block always reaches the right margin and a
        table's rows almost never do.
        """
        if not self.body_right:
            return False
        return abs(x1 - self.body_right) <= tolerance

    def starts_at_text_left(self, x0: float, tolerance: float = 2.0) -> bool:
        """Whether a block starts flush at the text margin, rather than indented.

        The complement of `reaches_text_width`, and the two together are what
        identify a wrapped line: the line before it ran to the right margin, and
        it begins at the left one. An indented line (LaTeX's `\\parindent`, 18pt
        in both corpus files) starts a new paragraph instead, and the tolerance
        is deliberately far tighter than that gap.
        """
        if not self.body_left:
            return False
        return abs(x0 - self.body_left) <= tolerance


def _block_lines(block: dict) -> list[str]:
    """The normalized text of each line in a block."""
    return [normalize_ws("".join(s["text"] for s in line["spans"])) for line in block["lines"]]


def block_text(block: dict, profile: DocumentProfile | None = None) -> str:
    """A block's full text as one string.

    The separator matters: joining a block's lines with nothing at all silently
    welds the last word of each line to the first of the next ("population
    livingin each"). With a `profile` the document's vocabulary also resolves
    line-break hyphens; without one, lines are simply joined by spaces.
    """
    lines = _block_lines(block)
    if profile is not None:
        return join_lines(lines, profile)
    return normalize_ws(" ".join(lines))


def _dominant_span(block: dict) -> tuple[str, float, int]:
    """The (font, size, flags) covering the most characters in a block.

    Taking the most common span rather than the first avoids a stray italic or
    footnote marker deciding a block's class.
    """
    weights: Counter[tuple[str, float, int]] = Counter()
    for line in block["lines"]:
        for span in line["spans"]:
            weights[(span["font"], round(span["size"], 1), span["flags"])] += len(span["text"])
    return weights.most_common(1)[0][0] if weights else ("", 0.0, 0)


def _math_share(block: dict) -> float:
    """Fraction of a block's characters set in a mathematics font."""
    math = total = 0
    for line in block["lines"]:
        for span in line["spans"]:
            n = len(span["text"].strip())
            total += n
            if _MATH_FONT.search(span["font"]):
                math += n
    return math / total if total else 0.0


def text_blocks(page: pymupdf.Page) -> list[dict]:
    """A page's text blocks in reading order.

    Both corpus files are single column, so sorting by vertical position and
    breaking ties horizontally is sufficient. The y coordinate is bucketed to
    3pt so that two blocks sharing a line are ordered left to right rather than
    by sub-point differences in their baselines.
    """
    blocks = [b for b in page.get_text("dict")["blocks"] if b["type"] == 0 and b.get("lines")]
    return sorted(blocks, key=lambda b: (round(b["bbox"][1] / 3), b["bbox"][0]))


def profile_document(doc: pymupdf.Document) -> DocumentProfile:
    """One pass over the document to gather what per-page classification needs."""
    sizes: Counter[float] = Counter()
    hyphenated: Counter[str] = Counter()
    words: Counter[str] = Counter()
    band_texts: Counter[str] = Counter()
    extents: Counter[tuple[float, float]] = Counter()
    edges: Counter[tuple[float, float]] = Counter()

    for page in doc:
        height = page.rect.height or 1.0
        for block in text_blocks(page):
            for line in block["lines"]:
                for span in line["spans"]:
                    # Weight by characters: a 3-word heading must not outvote a
                    # paragraph when deciding what "body text" means.
                    sizes[round(span["size"], 1)] += len(span["text"].strip())

            for text in _block_lines(block):
                for word in re.findall(r"[A-Za-z]+(?:-[A-Za-z]+)+", text):
                    hyphenated[word.casefold()] += 1
                for word in re.findall(r"[A-Za-z]+", text):
                    words[word.casefold()] += 1

            if len(block["lines"]) >= 3:
                # Three or more lines makes a block a paragraph beyond much
                # doubt, so its width is a fair sample of the text column.
                extents[(round(block["bbox"][0]), round(block["bbox"][2]))] += 1

            # Every block, whatever its height, for the left margin below: in a
            # double-spaced document each line is its own block, so a rule that
            # only looks at 3-line blocks never sees the body text at all.
            edges[(round(block["bbox"][0]), round(block["bbox"][2]))] += 1

            top = block["bbox"][1] / height
            if top >= _FOOTER_BAND or top <= _HEADER_BAND:
                joined = normalize_ws(" ".join(_block_lines(block)))
                if joined:
                    # Mask digits so "Chapter 4 | 71" and "Chapter 5 | 93"
                    # count as the same running header.
                    band_texts[_DIGITS.sub("#", joined)] += 1

    threshold = max(_REPEAT_MIN_PAGES, int(doc.page_count * _REPEAT_SHARE))
    _, right = extents.most_common(1)[0][0] if extents else (0.0, 0.0)
    return DocumentProfile(
        body_size=sizes.most_common(1)[0][0] if sizes else 12.0,
        page_count=doc.page_count,
        body_left=_text_left(edges, right),
        body_right=right,
        hyphenated=hyphenated,
        words=words,
        furniture_patterns={t for t, n in band_texts.items() if n >= threshold},
    )


def _text_left(edges: Counter[tuple[float, float]], right: float, tolerance: float = 8.0) -> float:
    """The left margin of the text column, measured over full-width blocks only.

    Sampling every block's left edge measures the wrong thing -- indented first
    lines and hanging reference indents outvote the margin itself in a
    double-spaced document. Blocks that reach the right margin are wrapped
    prose by construction, and where they start is the margin.
    """
    lefts: Counter[float] = Counter()
    for (left, block_right), n in edges.items():
        if right and abs(block_right - right) <= tolerance:
            lefts[left] += n
    return lefts.most_common(1)[0][0] if lefts else 0.0


def join_lines(lines: list[str], profile: DocumentProfile) -> str:
    """Join a block's lines into one string, resolving line-break hyphens.

    When a line ends in a hyphen, the document's own vocabulary decides:
    the compound wins if it is attested hyphenated elsewhere, otherwise the
    merged word wins if *it* is attested, otherwise we join -- which is the
    common case by roughly four to one in this corpus.
    """
    if not lines:
        return ""

    out = lines[0]
    for nxt in lines[1:]:
        head = re.search(r"([A-Za-z]+)-$", out)
        tail = re.match(r"([A-Za-z]+)", nxt)
        if head and tail:
            compound = f"{head.group(1)}-{tail.group(1)}".casefold()
            merged = f"{head.group(1)}{tail.group(1)}".casefold()
            if profile.hyphenated.get(compound, 0) > 0 and not profile.words.get(merged, 0):
                out = f"{out}{nxt}"  # a real compound: keep the hyphen, drop the break
                continue
            out = f"{out[:-1]}{nxt}"  # drop the hyphen and close the word up
            continue
        out = f"{out} {nxt}"
    return normalize_ws(out)


def classify(block: dict, profile: DocumentProfile, page_height: float) -> str:
    """Decide what a text block is. Order matters: furniture first, so a page
    number is never mistaken for a one-word heading."""
    lines = _block_lines(block)
    joined = normalize_ws(" ".join(lines))
    if not joined:
        return "furniture"

    top = block["bbox"][1] / page_height
    in_band = top >= _FOOTER_BAND or top <= _HEADER_BAND
    if in_band and (
        _BARE_NUMBER.match(joined) or _DIGITS.sub("#", joined) in profile.furniture_patterns
    ):
        return "furniture"

    if CAPTION_RE.match(joined):
        return "caption"

    if _math_share(block) >= 0.4:
        return "equation"

    _, size, flags = _dominant_span(block)
    if profile.is_heading_size(size) and not (flags & _FLAG_ITALIC):
        return "heading"

    return "paragraph"


def extract_text_elements(
    page: pymupdf.Page,
    page_label: str,
    profile: DocumentProfile,
    exclude: list[pymupdf.Rect] | None = None,
) -> list[Element]:
    """Every text element on one page, in reading order.

    `exclude` carries the regions already claimed by a table or a figure.
    Without it a table's contents appear twice -- once mangled into prose, once
    as a table -- which is the single most common way a parsed corpus quietly
    doubles its own weight.
    """
    exclude = exclude or []
    height = page.rect.height or 1.0
    elements: list[Element] = []

    for block in text_blocks(page):
        bbox = pymupdf.Rect(block["bbox"])
        if any(_claimed_by(bbox, region) for region in exclude):
            continue

        kind = classify(block, profile, height)
        text = join_lines(_block_lines(block), profile)
        if not text:
            continue

        elements.append(
            Element(
                type=kind,  # type: ignore[arg-type]
                text=text,
                page_index=page.number,
                page_label=page_label,
                bbox=(bbox.x0, bbox.y0, bbox.x1, bbox.y1),
            )
        )

    return elements


def _claimed_by(bbox: pymupdf.Rect, region: pymupdf.Rect) -> bool:
    """Whether a text block belongs to a table or figure region.

    Compares centres rather than requiring containment: an extracted table's
    bounding box is usually a point or two tighter than the text it holds.
    """
    centre = pymupdf.Point((bbox.x0 + bbox.x1) / 2, (bbox.y0 + bbox.y1) / 2)
    return centre in region


def headings_of(elements: list[Element]) -> list[Element]:
    return [e for e in elements if e.type == "heading"]


__all__ = [
    "CAPTION_RE",
    "BBox",
    "DocumentProfile",
    "block_text",
    "classify",
    "extract_text_elements",
    "headings_of",
    "join_lines",
    "normalize_ws",
    "profile_document",
    "text_blocks",
]
