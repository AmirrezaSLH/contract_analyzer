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

#: The document's vocabulary. Tokens may contain digits so that identifiers
#: such as "PASS-02" or "ISO 27001" are attested like any other word.
_TOKEN = re.compile(r"[A-Za-z0-9]+")
_COMPOUND = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+")
#: The word fragment before a line-final hyphen, and the one starting the next
#: line. Digits are allowed on both sides: "GOV-" + "01" is the common case in
#: a control matrix.
_HEAD = re.compile(r"([A-Za-z0-9]+)-$")
_TAIL = re.compile(r"([A-Za-z0-9]+)")
#: The last and first whole token of two lines with no hyphen between them.
_LAST_TOKEN = re.compile(r"([A-Za-z]+)$")
_FIRST_TOKEN = re.compile(r"^([A-Za-z]+)")

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
    #: Tokens seen hyphenated somewhere in the document ("single-family",
    #: "PASS-02"). Letter-digit compounds are collected too, so a control
    #: identifier that appears unwrapped anywhere attests its own shape.
    hyphenated: Counter[str] = field(default_factory=Counter)
    #: Every token seen, unhyphenated ("building", "27001").
    words: Counter[str] = field(default_factory=Counter)
    #: Whether this document hyphenates words at line ends. Measured, not
    #: assumed: LaTeX does (so an unattested line-final hyphen is typographic
    #: and should be dropped), Word does not (so it is part of the word and
    #: must be kept). See `infer_breaks_hyphenate`.
    breaks_hyphenate: bool = True
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
    breaks: list[tuple[str, str]] = []

    for page in doc:
        height = page.rect.height or 1.0
        for block in text_blocks(page):
            for line in block["lines"]:
                for span in line["spans"]:
                    # Weight by characters: a 3-word heading must not outvote a
                    # paragraph when deciding what "body text" means.
                    sizes[round(span["size"], 1)] += len(span["text"].strip())

            lines = _block_lines(block)
            for text in lines:
                for word in _COMPOUND.findall(text):
                    hyphenated[word.casefold()] += 1
                for word in _TOKEN.findall(text):
                    words[word.casefold()] += 1
            for line, nxt in zip(lines, lines[1:], strict=False):
                head = _HEAD.search(line)
                tail = _TAIL.match(nxt)
                if head and tail:
                    breaks.append((head.group(1), tail.group(1)))

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
        breaks_hyphenate=infer_breaks_hyphenate(breaks, hyphenated, words),
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


def infer_breaks_hyphenate(
    breaks: list[tuple[str, str]],
    hyphenated: Counter[str] | dict[str, int],
    words: Counter[str] | dict[str, int],
) -> bool:
    """Whether a document hyphenates at line ends, judged from its own breaks.

    For every line-final hyphen, the vocabulary is asked which form of the
    broken word it attests elsewhere: the merged word (``build-`` + ``ing``
    with ``building`` in the text -- a typographic break) or the compound
    (``single-`` + ``family`` with ``single-family`` -- a lexical hyphen).
    A document whose merged forms dominate auto-hyphenates; one whose
    compounds dominate does not. With no evidence either way the hyphen is
    kept, because that is the reversible choice.
    """
    merged = compound = 0
    for head, tail in breaks:
        if words.get(f"{head}{tail}".casefold(), 0):
            merged += 1
        if hyphenated.get(f"{head}-{tail}".casefold(), 0):
            compound += 1
    return merged > compound


def join_lines(lines: list[str], profile: DocumentProfile) -> str:
    """Join a block's lines into one string, resolving the breaks between them.

    Every decision is taken from evidence the document supplies about itself.
    At a line-final hyphen, in order:

    1. the compound is attested and the merged word is not -- a real compound,
       keep the hyphen (``single-family``);
    2. the hyphen sits at a letter/digit boundary -- no language hyphenates a
       word there, so it is part of a token: keep it (``GOV-01``, ``ISO-27001``);
    3. the merged word is attested -- a typographic break, drop the hyphen
       (``building``);
    4. otherwise follow the document's measured habit (`breaks_hyphenate`).

    At a line end with no hyphen, the two lines are concatenated without a
    space only when the result is an attested word and neither fragment is a
    word on its own: ``Monitoring/Alertin`` + ``g`` closes up, ``Special`` +
    ``Handling`` does not, and ``a`` + ``gain`` does not either.
    """
    if not lines:
        return ""

    out = lines[0]
    for nxt in lines[1:]:
        head = _HEAD.search(out)
        tail = _TAIL.match(nxt)
        if head and tail:
            resolved = _resolve_hyphen(head.group(1), tail.group(1), profile)
            out = f"{out[: head.start()]}{resolved}{nxt[tail.end() :]}"
            continue
        if _is_hard_wrap(out, nxt, profile):
            out = f"{out}{nxt}"
            continue
        out = f"{out} {nxt}"
    return normalize_ws(out)


def _resolve_hyphen(head: str, tail: str, profile: DocumentProfile) -> str:
    compound = f"{head}-{tail}"
    merged = f"{head}{tail}"
    if profile.hyphenated.get(compound.casefold(), 0) and not profile.words.get(
        merged.casefold(), 0
    ):
        return compound
    if head[-1].isdigit() != tail[0].isdigit():
        return compound  # a letter/digit boundary is never a typographic break
    if profile.words.get(merged.casefold(), 0):
        return merged
    return merged if profile.breaks_hyphenate else compound


def _is_hard_wrap(out: str, nxt: str, profile: DocumentProfile) -> bool:
    """Whether a line ended mid-word with no hyphen to say so.

    A narrow table cell does this ("Requiremen" / "t Ref"). The document's
    vocabulary decides: the concatenation must be attested strictly more often
    than the longer of the two fragments. A fragment always attests itself at
    least once (the wrapped cell is in the vocabulary too), so "alerting" at 8
    beats "alertin" at 2, while two real words are never welded because a
    real word is at least as common as whatever it happens to spell when
    glued to its neighbour. The shorter fragment is not consulted: a stub
    like "g" or "t" is also a frequent token in its own right ("e.g.").
    """
    last = _LAST_TOKEN.search(out)
    first = _FIRST_TOKEN.match(nxt)
    if not last or not first:
        return False
    longer = max(last.group(1), first.group(1), key=len)
    joined = profile.words.get(f"{last.group(1)}{first.group(1)}".casefold(), 0)
    return joined > profile.words.get(longer.casefold(), 0)


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
    "infer_breaks_hyphenate",
    "join_lines",
    "normalize_ws",
    "profile_document",
    "text_blocks",
]
