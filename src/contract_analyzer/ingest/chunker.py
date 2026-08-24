"""Elements become chunks: the units that get embedded, indexed and cited.

The parser hands over a contract as an ordered list of meaning-bearing
elements -- and, since the hardening pass, a list with properties the chunker
can rely on rather than reconstruct: every element carries a section
breadcrumb, a numbered clause is a standalone element instead of being welded
into its neighbour, and a table that a page break split has been stitched back
into one grid. That makes the chunker's job smaller than it would otherwise
be. Most chunks here are exactly one clause.

The rules, each of them a measurement of the sample contract rather than a
default:

* a chunk is a run of prose from **one section**, packed to the token budget
  and overlapped -- so no chunk begins mid-sentence, and no chunk's citation
  names a section it is only half in;
* a table is a chunk of its own: half a table is worse than none, and the
  requirement matrices in the exhibits are the evidence a compliance question
  is answered from;
* a figure is a chunk of its own too, with the caption and the prose that
  cites it;
* an equation is never a chunk alone -- two tokens of algebra retrieve
  nothing by themselves and everything when attached to the prose around them;
* a heading is never a chunk. It is already in `section_path`, and a hundred
  near-empty rows would compete in BM25 with the sections they name.

**Nothing is dropped by name.** This is the rule carried over from the parser:
a decision may only use evidence the document supplies about itself. A
contract's exhibits, schedules and annexes *are* the contract -- Exhibit G is
where the password requirements live -- and "Definitions" is what a defined
term resolves to. The only elements refused an index entry are ones that are
pointers to content rather than content: a contents row with a dot leader,
and, when the section spine came from the PDF's own outline and can therefore
be trusted, a section the outline itself names as a map of the document. When
the spine was *synthesized* from headings and clause numbering, a "Contents"
heading owns every element until the next detected heading, so dropping by
that title would take real clauses with it. See `_select`.

Every chunk's `content` opens with its section breadcrumb, tables included. A
chunk is read alone -- by BM25, by the embedder, by the answer model -- and
"6. Identity, Access, Authentication, and Password Management > 6.6 Password
Management Standard" is what tells the reader which obligation they are
looking at. It is also what lets a keyword search for "password management"
reach a requirements row whose own cells never say those words. The budget is
reduced by the breadcrumb rather than exceeded by it.

The output is a pure function of (elements, settings): same input, same
chunks, byte for byte. Idempotent ingestion depends on it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import PROJECT_ROOT, Settings
from ..models import Chunk
from ..parse.elements import Element, ElementType, FigureElement, TableElement
from ..parse.outline import normalize_title, strip_leading_number
from ..parse.pdf import ParsedDocument
from ..parse.tables import rows_to_markdown
from ..tokens import count_tokens

#: A chunk shorter than this is a fragment rather than an obligation. Set low
#: on purpose: a real clause can be one sentence ("9.2 Termination for
#: Convenience. Either party may terminate on thirty days' written notice.")
#: and a floor tuned for prose would delete it. The report counts what the
#: floor removes, and on the sample contract that count is zero -- which is
#: the evidence that the floor is not quietly costing us a clause.
MIN_CHUNK_TOKENS = 8

#: Pieces are joined by a newline, which the tokenizer charges for.
_JOIN_TOKENS = 1

#: The smallest budget a breadcrumb may leave for the text itself. Breadcrumbs
#: on this contract reach ~25 tokens, so the budget is never near this floor.
_MIN_BUDGET = 64

#: How much of a referencing paragraph is quoted into a figure chunk.
_REFERENCE_SNIPPET_TOKENS = 120

#: A contents row: a dot leader with the page number it points at. Deliberately
#: tighter than the parser's `_DOT_LEADER`, which only has to decide whether to
#: *join* two lines. Here the element is deleted, so an ellipsis inside a
#: quoted definition must not match.
_DOT_LEADER_ROW = re.compile(r"(?:\.\s?){4,}\.?\s*[\divxlcdm]+\s*$", re.I)

#: Sections that are a map of the document rather than part of it. Matched on
#: the normalized title, at any depth of `section_path`, and only when the
#: spine came from the PDF's own outline -- see the module docstring.
_FRONT_MATTER_SECTIONS = frozenset(
    {"contents", "table of contents", "list of figures", "list of tables"}
)

#: "Figure 3.1", "Fig. 2", "Table 5.1" -- as a caption opens and as prose
#: mentions it. Used to join a figure to the paragraph that discusses it.
_LABEL = re.compile(
    r"\b(fig|figs|figure|figures|table|tables)\b\.?\s*([A-Za-z]?\d+(?:\.\d+)*)", re.I
)
_CAPTION_LABEL = re.compile(r"^\s*(fig|figure|table)\b\.?\s*([A-Za-z]?\d+(?:\.\d+)*)", re.I)

#: A sentence boundary: terminal punctuation, optional closing quote, then
#: space. Reached when an element is over budget, and when overlap has to be
#: taken from part of a clause rather than all of it.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])[\"')\]]*\s+")

#: Words that end in a full stop without ending a sentence. Contract prose
#: breaks on the corporate suffixes and the cross-reference forms; a lone
#: capital is an initial in a signature block.
_ABBREVIATIONS = frozenset(
    {
        "al", "app", "approx", "art", "cf", "cl", "co", "corp", "dr", "e.g",
        "ex", "etc", "fig", "figs", "i.e", "inc", "llc", "llp", "ltd", "max",
        "min", "mr", "mrs", "ms", "no", "nos", "para", "paras", "p", "pp",
        "sched", "sec", "sect", "st", "tab", "u.s", "vs", "vol",
    }
)


@dataclass
class ChunkingReport:
    """What the chunker dropped and why, for the ingest report to print.

    Silent deletion is the hardest kind of bug to see from the outside: a
    contract that lost its exhibits and a contract that never had any look
    identical in the database. Every counter here is asserted against the
    sample contract in the test suite, so the numbers are claims, not notes.
    """

    elements_in: int = 0
    headings: int = 0
    dropped_contents: int = 0
    dropped_front_matter: int = 0
    dropped_short_chunks: int = 0
    oversized_elements_split: int = 0
    tables_split: int = 0
    figures_with_reference: int = 0
    figures_total: int = 0
    #: Chunks that opened with context carried from the one before them, and
    #: the tokens carried in total. The pair is how the configured overlap is
    #: checked against the overlap the document actually got -- see
    #: `_overlap_seed` for why those are not the same number.
    overlap_chunks: int = 0
    overlap_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(vars(self))


@dataclass(frozen=True)
class _Piece:
    """An element, or one slice of an element too big to be a chunk on its own."""

    element: Element
    text: str
    tokens: int
    #: True when the piece was carried over from the previous chunk as overlap.
    #: Such a piece supplies context, never the citation: the page and section a
    #: chunk claims come from its own first element.
    overlap: bool = False


@dataclass
class _Draft:
    """A chunk before its breadcrumb and its ordinal are attached."""

    element_type: ElementType
    anchor: Element
    body: str
    payload: str | None = None
    asset_path: str | None = None
    pieces: list[_Piece] = field(default_factory=list)


def chunk_document(
    parsed: ParsedDocument,
    settings: Settings,
    *,
    report: ChunkingReport | None = None,
) -> list[Chunk]:
    """Chunk a parsed document. Pass a `ChunkingReport` to learn what was dropped."""
    return chunk_elements(
        parsed.elements, settings, spine_source=parsed.spine_source, report=report
    )


def chunk_elements(
    elements: list[Element],
    settings: Settings,
    *,
    spine_source: str = "none",
    report: ChunkingReport | None = None,
) -> list[Chunk]:
    """The chunker proper.

    `list[Element]` rather than `ParsedDocument` is the contract on purpose: a
    `.docx` or a `.md` loader produces elements too, and everything downstream
    of this function is then identical regardless of where the document came
    from. `spine_source` is the one fact about the *document* that changes a
    decision here, so it is passed explicitly rather than inferred.
    """
    report = report if report is not None else ChunkingReport()
    report.elements_in = len(elements)

    kept = _select(elements, spine_source=spine_source, report=report)
    mentions = _mention_map(kept)

    drafts: list[_Draft] = []
    run: list[Element] = []
    run_path: tuple[str, ...] | None = None

    def flush() -> None:
        nonlocal run, run_path
        if run:
            drafts.extend(_pack_prose(run, settings, report))
        run = []
        run_path = None

    for element in kept:
        kind = _kind(element)
        if kind in {"table", "figure"}:
            # Atomic, and never packed with the prose on either side of it.
            flush()
            drafts.extend(_atomic_drafts(element, settings, mentions, report))
            continue
        path = tuple(element.section_path)
        if run_path is not None and path != run_path:
            # Mixing two sections in one chunk makes its citation misleading.
            flush()
        run_path = path
        run.append(element)
    flush()

    return _render(drafts, report)


# --------------------------------------------------------------------------
# Stage A -- filter
# --------------------------------------------------------------------------


def _select(
    elements: list[Element], *, spine_source: str, report: ChunkingReport
) -> list[Element]:
    """Drop what should not be indexed, counting each reason.

    The list of reasons is deliberately short. A contents row is a pointer to
    a clause that is itself indexed, so retrieving the row instead of the
    clause is a failure. A front-matter *section* is the same thing one level
    up -- but only the PDF's own outline is trusted to name one, because a
    synthesized spine gives a "Contents" heading ownership of everything until
    the next heading it managed to detect.
    """
    kept: list[Element] = []
    trust_titles = spine_source == "outline"

    for element in elements:
        if element.type == "furniture":
            continue

        if element.type == "heading":
            # Context, not content: it is already in every chunk's breadcrumb.
            report.headings += 1
            continue

        if _DOT_LEADER_ROW.search(element.text):
            report.dropped_contents += 1
            continue

        if trust_titles and _in_sections(element, _FRONT_MATTER_SECTIONS):
            report.dropped_front_matter += 1
            continue

        kept.append(element)

    return kept


def _in_sections(element: Element, names: frozenset[str]) -> bool:
    return any(_is_title(title, names) for title in element.section_path)


def _is_title(text: str, names: frozenset[str]) -> bool:
    """Whether a heading or outline title names one of `names`.

    Normalized the same way the outline is matched to the page, so "CONTENTS"
    and "Section 1 Contents" both land on "contents".
    """
    return normalize_title(strip_leading_number(text)) in names


def _kind(element: Element) -> str:
    if isinstance(element, TableElement) or element.type == "table":
        return "table"
    if isinstance(element, FigureElement) or element.type == "figure":
        return "figure"
    if element.type == "equation":
        return "equation"
    # A caption that outlived the thing it captions. It is real text about real
    # content, so it packs as prose rather than vanishing.
    return "prose"


# --------------------------------------------------------------------------
# Stage B -- pack prose
# --------------------------------------------------------------------------


def _pack_prose(
    run: list[Element], settings: Settings, report: ChunkingReport
) -> list[_Draft]:
    """Pack one run of same-section prose into drafts.

    A chunk closes when the next element would exceed the budget; the next
    chunk then re-opens with the tail of the one just closed, up to
    `chunk_overlap_tokens`.
    """
    budget = _budget(run[0], settings)
    overlap_budget = min(settings.chunk_overlap_tokens, budget // 2)

    groups: list[list[_Piece]] = []
    current: list[_Piece] = []
    fresh = 0  # pieces added since the last close: never close on zero progress

    for element in run:
        for piece in _pieces(element, budget, report):
            equation = piece.element.type == "equation"
            over = _total(current) + _JOIN_TOKENS + piece.tokens > budget
            # An equation never closes a chunk: two tokens of algebra are not a
            # reason to start a new retrieval unit, and it must not be left
            # standing alone.
            if current and fresh and over and not equation:
                groups.append(current)
                current = _overlap_seed(current, overlap_budget)
                if current:
                    report.overlap_chunks += 1
                    report.overlap_tokens += sum(p.tokens for p in current)
                fresh = 0
            current.append(piece)
            fresh += 1

    if current and fresh:
        groups.append(current)

    drafts: list[_Draft] = []
    for group in _merge_equation_only(groups):
        draft = _prose_draft(group)
        if draft is not None:
            drafts.append(draft)
    return drafts


def _total(pieces: list[_Piece]) -> int:
    """Tokens in a group of pieces, counting the newline that will join them.

    A separator is one token each, and ignoring them is how a 400-token budget
    quietly becomes 410.
    """
    return sum(p.tokens for p in pieces) + max(len(pieces) - 1, 0) * _JOIN_TOKENS


def _budget(element: Element, settings: Settings) -> int:
    """The token budget left for text once the breadcrumb has had its share."""
    breadcrumb = element.breadcrumb
    spend = count_tokens(breadcrumb) + 1 if breadcrumb else 0
    return max(settings.chunk_tokens - spend, _MIN_BUDGET)


def _pieces(element: Element, budget: int, report: ChunkingReport) -> list[_Piece]:
    """An element as one piece, or as several if it cannot fit in a chunk."""
    text = element.text.strip()
    tokens = count_tokens(text)
    if tokens <= budget:
        return [_Piece(element=element, text=text, tokens=tokens)]
    report.oversized_elements_split += 1
    return [
        _Piece(element=element, text=part, tokens=count_tokens(part))
        for part in _split_text(text, budget)
    ]


def _overlap_seed(group: list[_Piece], overlap_budget: int) -> list[_Piece]:
    """The tail of a closed chunk, to open the next one with.

    Whole elements first: after the parser's wrapped-line join an element *is*
    a sentence-safe unit, so carrying one costs nothing in coherence.

    The fallback is the part that matters on a contract. Carrying only whole
    elements was free on a corpus whose elements were single wrapped lines of
    ~20 tokens, where three or four always fitted the budget. Here an element
    is a clause and the longest runs to ~290 tokens, so a strict whole-element
    rule would carry *nothing* after a big clause -- the configured 20% would
    be 0% exactly where the context is most worth having. So an element too
    big to carry whole contributes its trailing sentences instead, which
    satisfies the property whole-element overlap was standing in for: a chunk
    never begins mid-sentence. If even the last sentence is over budget,
    nothing is carried; a cut sentence is worse than no overlap.
    """
    if overlap_budget <= 0:
        return []
    seed: list[_Piece] = []
    total = 0
    # Never carry the whole chunk: the next one must contain something new, or
    # packing would not advance. That is what `group[1:]` is for.
    for piece in reversed(group[1:]):
        room = overlap_budget - total
        if piece.tokens + _JOIN_TOKENS <= room:
            seed.insert(
                0,
                _Piece(element=piece.element, text=piece.text, tokens=piece.tokens, overlap=True),
            )
            total += piece.tokens + _JOIN_TOKENS
            continue
        tail = _sentence_tail(piece.text, room - _JOIN_TOKENS)
        if tail:
            seed.insert(
                0,
                _Piece(
                    element=piece.element, text=tail, tokens=count_tokens(tail), overlap=True
                ),
            )
        break
    return seed


def _sentence_tail(text: str, budget: int) -> str:
    """The last whole sentences of `text` that fit in `budget` tokens."""
    if budget <= 0:
        return ""
    taken: list[str] = []
    total = 0
    for sentence in reversed(_sentences(text)):
        tokens = count_tokens(sentence) + _JOIN_TOKENS
        if total + tokens > budget:
            break
        taken.insert(0, sentence)
        total += tokens
    return " ".join(taken)


def _merge_equation_only(groups: list[list[_Piece]]) -> list[list[_Piece]]:
    """Fold a chunk that is nothing but equations into the one before it."""
    merged: list[list[_Piece]] = []
    for group in groups:
        if merged and all(p.element.type == "equation" for p in group):
            merged[-1].extend(group)
            continue
        merged.append(group)
    return merged


def _prose_draft(group: list[_Piece]) -> _Draft | None:
    if not group:
        return None
    # The citation belongs to this chunk's own first element, not to the
    # trailing context borrowed from the previous one.
    anchor = next((p.element for p in group if not p.overlap), group[0].element)
    kind: ElementType = (
        "equation" if all(p.element.type == "equation" for p in group) else "paragraph"
    )
    return _Draft(
        element_type=kind,
        anchor=anchor,
        body="\n".join(p.text for p in group),
        pieces=list(group),
    )


# --------------------------------------------------------------------------
# Stage C -- tables and figures
# --------------------------------------------------------------------------


def _atomic_drafts(
    element: Element,
    settings: Settings,
    mentions: dict[str, Element],
    report: ChunkingReport,
) -> list[_Draft]:
    if isinstance(element, TableElement):
        return _table_drafts(element, settings, report)
    if isinstance(element, FigureElement):
        report.figures_total += 1
        return [_figure_draft(element, mentions, report)]
    return []


def _table_drafts(
    table: TableElement, settings: Settings, report: ChunkingReport
) -> list[_Draft]:
    """A table is one chunk, unless it is too big to be one.

    `body` is the grid; `_render` puts the breadcrumb in front of it, which is
    how a requirements matrix in an exhibit is findable by the section that
    names what it requires -- the cells themselves say "Password rotation",
    never "Password Management Standard". `payload` stays the bare markdown so
    the UI renders a grid rather than re-parsing a heading off the front of it.

    Splitting an over-budget table by rows with the header repeated keeps every
    part readable on its own; the alternative is a grid silently truncated
    wherever the budget happened to fall.
    """
    budget = _budget(table, settings)
    payload = table.markdown or None
    if count_tokens(table.text) <= budget:
        return [_Draft(element_type="table", anchor=table, body=table.text, payload=payload)]

    parts = _split_table_rows(table, budget)
    if not parts:
        # A text-fallback table has no grid to split; fall back to the text.
        parts = _split_text(table.text, budget)
        payload = None
    report.tables_split += 1

    drafts: list[_Draft] = []
    for index, part in enumerate(parts, start=1):
        marker = f"(part {index}/{len(parts)})"
        caption = f"{table.caption} {marker}".strip() if table.caption else marker
        drafts.append(
            _Draft(
                element_type="table",
                anchor=table,
                body=f"{caption}\n{part}",
                payload=part if payload is not None else None,
            )
        )
    return drafts


def _split_table_rows(table: TableElement, budget: int) -> list[str]:
    """Grids of at most `budget` tokens, each carrying the header row."""
    if len(table.rows) < 3 or not table.markdown:
        return []
    header, body = table.rows[0], table.rows[1:]
    overhead = count_tokens(table.caption) + 8  # caption plus the part marker

    parts: list[list[list[str]]] = []
    current: list[list[str]] = []
    for row in body:
        if current and count_tokens(rows_to_markdown([header, *current, row])) + overhead > budget:
            parts.append(current)
            current = [row]
        else:
            current.append(row)
    if current:
        parts.append(current)
    return [rows_to_markdown([header, *part]) for part in parts]


def _figure_draft(
    figure: FigureElement, mentions: dict[str, Element], report: ChunkingReport
) -> _Draft:
    """Caption, description if one was generated, and the prose that cites it.

    A caption alone is true and nearly unretrievable. The paragraph that refers
    to the figure by number is where the words a reader would actually search
    for live, so a sentence or two of it is quoted into the chunk.
    """
    lines = [figure.text.strip()]
    if figure.description:
        lines.append(figure.description.strip())

    referencing = _referencing_element(figure, mentions)
    if referencing is not None:
        report.figures_with_reference += 1
        lines.append(f"Referenced in the text: {_snippet(referencing, figure)}")

    panels = [_relative(path) for path in figure.asset_paths]
    payload = json.dumps(
        {"panels": panels, "description": figure.description}, ensure_ascii=False
    )
    return _Draft(
        element_type="figure",
        anchor=figure,
        body="\n".join(line for line in lines if line),
        payload=payload,
        asset_path=panels[0] if panels else None,
    )


# --------------------------------------------------------------------------
# Stage D -- attach a figure to the paragraph that references it
# --------------------------------------------------------------------------


def _mention_map(elements: list[Element]) -> dict[str, Element]:
    """`"figure:3.1"` -> the first paragraph that mentions it.

    The parser stops at one element per thing on the page; resolving "Figure
    3.1" to the prose that discusses it spans elements, so it lands here. Only
    prose is scanned, which is what keeps a caption from matching itself.
    """
    mentions: dict[str, Element] = {}
    for element in elements:
        if _kind(element) != "prose":
            continue
        for match in _LABEL.finditer(element.text):
            mentions.setdefault(_label_key(match.group(1), match.group(2)), element)
    return mentions


def _referencing_element(
    figure: FigureElement, mentions: dict[str, Element]
) -> Element | None:
    match = _CAPTION_LABEL.match(figure.caption or figure.text)
    if match is None:
        return None
    return mentions.get(_label_key(match.group(1), match.group(2)))


def _label_key(kind: str, number: str) -> str:
    family = "table" if kind.lower().startswith("tab") else "figure"
    return f"{family}:{number.lower()}"


def _snippet(element: Element, figure: FigureElement) -> str:
    """The part of a referencing paragraph worth quoting into a figure chunk.

    A packed clause can run to several hundred tokens, which would leave the
    figure a minority of its own chunk. The sentence containing the reference,
    plus the one after it, is the part that is actually about the figure.
    """
    text = element.text.strip()
    if count_tokens(text) <= _REFERENCE_SNIPPET_TOKENS:
        return text

    sentences = _sentences(text)
    match = _CAPTION_LABEL.match(figure.caption or figure.text)
    key = _label_key(match.group(1), match.group(2)) if match else ""
    start = 0
    for index, sentence in enumerate(sentences):
        if any(_label_key(m.group(1), m.group(2)) == key for m in _LABEL.finditer(sentence)):
            start = index
            break

    taken: list[str] = []
    total = 0
    for sentence in sentences[start:]:
        tokens = count_tokens(sentence)
        if taken and total + tokens > _REFERENCE_SNIPPET_TOKENS:
            break
        taken.append(sentence)
        total += tokens
    return " ".join(taken)


# --------------------------------------------------------------------------
# Splitting text that has no element boundary to split on
# --------------------------------------------------------------------------


def _split_text(text: str, budget: int) -> list[str]:
    """`text` as parts of at most `budget` tokens, split at sentence ends."""
    parts: list[str] = []
    current: list[str] = []
    total = 0
    for sentence in _sentences(text):
        tokens = count_tokens(sentence) + _JOIN_TOKENS
        if current and total + tokens > budget:
            parts.append(" ".join(current))
            current, total = [], 0
        if tokens > budget:
            # One sentence longer than a whole chunk: a table rendered as a
            # line, usually. Cut it on whitespace rather than lose it.
            parts.extend(_split_words(sentence, budget))
            continue
        current.append(sentence)
        total += tokens
    if current:
        parts.append(" ".join(current))
    return parts or [text]


def _sentences(text: str) -> list[str]:
    """Sentences, without breaking on "Ex. 3" or "Acme Holdings, Inc."."""
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_BREAK.finditer(text):
        head = text[start : match.start()]
        if _ends_in_abbreviation(head):
            continue
        if head.strip():
            sentences.append(head.strip())
            start = match.end()
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences or [text.strip()]


def _ends_in_abbreviation(head: str) -> bool:
    match = re.search(r"([A-Za-z][A-Za-z.]*)\.$", head.rstrip("\"')]"))
    if match is None:
        return False
    word = match.group(1)
    # A single letter is an initial in a signature block, never a sentence end.
    return len(word) == 1 or word.lower() in _ABBREVIATIONS


def _split_words(text: str, budget: int) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    total = 0
    for word in text.split():
        tokens = count_tokens(word) + 1
        if current and total + tokens > budget:
            parts.append(" ".join(current))
            current, total = [], 0
        current.append(word)
        total += tokens
    if current:
        parts.append(" ".join(current))
    return parts


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _render(drafts: list[_Draft], report: ChunkingReport) -> list[Chunk]:
    chunks: list[Chunk] = []
    for draft in drafts:
        body_tokens = count_tokens(draft.body)
        if draft.element_type not in {"table", "figure"} and body_tokens < MIN_CHUNK_TOKENS:
            # The floor is on the text, not on the text plus its breadcrumb:
            # otherwise a long section title would keep a three-word chunk.
            report.dropped_short_chunks += 1
            continue
        anchor = draft.anchor
        breadcrumb = anchor.breadcrumb
        content = f"{breadcrumb}\n{draft.body}" if breadcrumb else draft.body
        page_end, page_label_end = _page_end(draft)
        chunks.append(
            Chunk(
                ordinal=len(chunks),
                content=content,
                page=anchor.page_index,
                page_label=anchor.page_label,
                page_end=page_end,
                page_label_end=page_label_end,
                section=anchor.section,
                section_path=list(anchor.section_path),
                element_type=draft.element_type,
                bbox=anchor.bbox,
                asset_path=draft.asset_path,
                payload=draft.payload,
                token_count=count_tokens(content),
            )
        )
    return chunks


def _page_end(draft: _Draft) -> tuple[int | None, str]:
    """The last page this chunk's own text reaches, or (None, "") for one page.

    The union runs over the chunk's non-overlap pieces: overlap is context
    borrowed from earlier pages and must not widen the range a citation shows,
    while a clause the parser rejoined across a page break, or a table it
    stitched, genuinely continues onto the next one. A chunk anchored on a
    clause that starts on page 9 and ends on page 10 cites "p.9-10", which is
    the difference between sending a reviewer to the quoted obligation and
    sending them to the page before it.
    """
    own = [p.element for p in draft.pieces if not p.overlap] or [draft.anchor]
    last = max(own, key=lambda e: e.page_span[1])
    end = last.page_span[1]
    if end <= draft.anchor.page_index:
        return None, ""
    return end, last.page_label_end or last.page_label


def _relative(path: Path) -> str:
    """An asset path relative to the project root, so the database stays portable."""
    try:
        return str(Path(path).resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


__all__ = ["MIN_CHUNK_TOKENS", "ChunkingReport", "chunk_document", "chunk_elements"]
