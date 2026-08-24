"""Table extraction, as a ladder of decreasing confidence.

This is the hardest part of parsing a PDF, and the measurements say so. A table
is text positioned in a grid plus, if you are lucky, a few filled rectangles
for rules; the grid lives in the geometry, not in the data.

Two cases behave completely differently in this corpus:

* **Fully ruled** tables (Salehi p.50, 59 rule segments) are read perfectly by
  a line-based detector.
* **booktabs** tables -- the LaTeX default, with `\\toprule`/`\\midrule`/
  `\\bottomrule` and no vertical or cell borders -- give a line-based detector
  nothing to find, while a text-based detector applied to the whole page
  decides the entire page is one 47-column table.

So the ladder tries the reliable detector first, then recovers a clip for the
booktabs case, and -- crucially -- **validates**. A grid that does not survive
validation is stored as its own text in a fenced block rather than as data,
because a mangled grid presented as a table is worse than no table at all: a
reader can still read numbers out of a fenced block, but nobody can tell a
silently misaligned column from a real one.
"""

from __future__ import annotations

import pymupdf

from .blocks import CAPTION_RE, DocumentProfile, block_text, normalize_ws, text_blocks
from .elements import TableElement

#: A drawing this flat and this wide is a horizontal rule.
_RULE_MAX_HEIGHT = 2.0
_RULE_MIN_WIDTH = 100.0

#: How far outside the rules' horizontal span a block may sit and still count
#: as part of the table. Body prose overshoots by far more than this.
_SPAN_TOLERANCE = 6.0

#: A clip shorter than this cannot hold a header plus two data rows, which is
#: how a two-rule booktabs table (`\toprule` + `\midrule` only) betrays itself.
_MIN_CLIP_HEIGHT = 60.0

#: Validation thresholds. The fill rate is the one that does the work: a
#: recovered booktabs grid fails here precisely when its columns have drifted.
MIN_ROWS = 2
MIN_COLS = 2
MIN_FILL_RATE = 0.6


def horizontal_rules(page: pymupdf.Page) -> list[pymupdf.Rect]:
    return [
        d["rect"]
        for d in page.get_drawings()
        if d["rect"].height < _RULE_MAX_HEIGHT and d["rect"].width > _RULE_MIN_WIDTH
    ]


def table_captions(page: pymupdf.Page, profile: DocumentProfile | None = None) -> list[dict]:
    """`Table N.M:` caption blocks on a page. LaTeX sets these above the float."""
    out = []
    for block in text_blocks(page):
        text = block_text(block, profile)
        match = CAPTION_RE.match(text)
        if match and match.group(1).lower() == "table":
            out.append({"text": text, "bbox": pymupdf.Rect(block["bbox"])})
    return out


def compact(rows: list[list[str | None]]) -> list[list[str]]:
    """Drop wholly empty rows and columns, and normalise every cell.

    Both detectors emit padding: `strategy="text"` in particular returns a row
    per visual line, so a table with multi-line cells arrives interleaved with
    blank rows.
    """
    grid = [[normalize_ws(cell or "") for cell in row] for row in rows]
    grid = [row for row in grid if any(row)]
    if not grid:
        return []

    width = max(len(row) for row in grid)
    grid = [row + [""] * (width - len(row)) for row in grid]
    keep = [i for i in range(width) if any(row[i] for row in grid)]
    return [[row[i] for i in keep] for row in grid]


def validate(rows: list[list[str]]) -> bool:
    """Whether a grid is trustworthy enough to be stored as structured data."""
    if len(rows) < MIN_ROWS:
        return False
    width = len(rows[0])
    if width < MIN_COLS:
        return False
    if any(len(row) != width for row in rows):
        return False
    filled = sum(1 for row in rows for cell in row if cell)
    return filled / (len(rows) * width) >= MIN_FILL_RATE


def rows_to_markdown(rows: list[list[str]]) -> str:
    """Render a grid as a markdown table, first row as the header.

    Rendered here rather than via `Table.to_markdown()` so the output reflects
    the grid *after* compaction, and so newlines inside a cell become `<br>`
    instead of breaking the row.
    """
    if not rows:
        return ""
    escape = lambda cell: cell.replace("|", "\\|").replace("\n", "<br>")  # noqa: E731
    header, *body = rows
    lines = ["|" + "|".join(escape(c) for c in header) + "|"]
    lines.append("|" + "|".join("---" for _ in header) + "|")
    lines.extend("|" + "|".join(escape(c) for c in row) + "|" for row in body)
    return "\n".join(lines)


def recovery_clip(page: pymupdf.Page, caption_bbox: pymupdf.Rect | None) -> pymupdf.Rect | None:
    """The bounding box of a booktabs table, derived from its rules.

    The rules give the table's horizontal span but only part of its vertical
    extent -- a two-rule table yields a 39pt clip that misses most of its own
    body. The span is the useful half: body prose runs wider than the rules on
    at least one side, so the table is the run of blocks that fits *within* the
    span. That run gives the true bottom edge.
    """
    rules = horizontal_rules(page)
    if not rules:
        return None

    left = min(r.x0 for r in rules)
    right = max(r.x1 for r in rules)
    top = min(r.y0 for r in rules)
    if caption_bbox is not None and caption_bbox.y1 <= top + 40:
        top = min(top, caption_bbox.y1)

    run: list[pymupdf.Rect] = []
    for block in text_blocks(page):
        bbox = pymupdf.Rect(block["bbox"])
        if bbox.y1 < top - 2:
            continue
        if bbox.x0 >= left - _SPAN_TOLERANCE and bbox.x1 <= right + _SPAN_TOLERANCE:
            run.append(bbox)
        elif run:
            break  # the first block wider than the rules ends the table

    if not run:
        return None

    clip = pymupdf.Rect(
        min(b.x0 for b in run) - 4,
        top - 4,
        max(b.x1 for b in run) + 4,
        max(b.y1 for b in run) + 4,
    )
    return clip if clip.height >= _MIN_CLIP_HEIGHT else None


def caption_band(
    page: pymupdf.Page,
    caption_bbox: pymupdf.Rect,
    profile: DocumentProfile | None,
) -> pymupdf.Rect | None:
    """The region below a caption, for a table drawn with no rules whatsoever.

    `recovery_clip` needs at least one rule to establish the table's horizontal
    span, so a wholly unruled table (Sparks Tables A.7 and C.4) leaves it with
    nothing to work from. Here the run of blocks below the caption that do
    *not* reach the right margin is taken to be the table, and the first block
    that does reach it ends the run.
    """
    if profile is None or not profile.body_right:
        return None

    height = page.rect.height or 1.0
    run: list[pymupdf.Rect] = []
    for block in text_blocks(page):
        bbox = pymupdf.Rect(block["bbox"])
        if bbox.y0 < caption_bbox.y1 - 2:
            continue
        if bbox.y0 / height >= 0.85:
            break  # the footer: past the bottom of the text area
        if profile.reaches_text_width(bbox.x1):
            break  # ordinary prose resumed, so the table has ended
        run.append(bbox)

    if not run:
        return None
    return pymupdf.Rect(
        min(b.x0 for b in run) - 4,
        caption_bbox.y1 + 2,
        max(b.x1 for b in run) + 4,
        max(b.y1 for b in run) + 4,
    )


def _region_text(page: pymupdf.Page, clip: pymupdf.Rect) -> str:
    """The raw lines of a region, in order -- the honest fallback."""
    lines = []
    for block in text_blocks(page):
        bbox = pymupdf.Rect(block["bbox"])
        centre = pymupdf.Point((bbox.x0 + bbox.x1) / 2, (bbox.y0 + bbox.y1) / 2)
        if centre in clip:
            for line in block["lines"]:
                text = normalize_ws("".join(s["text"] for s in line["spans"]))
                if text:
                    lines.append(text)
    return "\n".join(lines)


def _build(
    page: pymupdf.Page,
    page_label: str,
    bbox: pymupdf.Rect,
    caption: str,
    rows: list[list[str]],
    quality: str,
    caption_bbox: pymupdf.Rect | None = None,
) -> TableElement:
    if quality == "text-fallback":
        body = f"```\n{_region_text(page, bbox)}\n```"
        markdown = ""
    else:
        markdown = rows_to_markdown(rows)
        body = markdown

    # The caption is what carries the semantics: a bare markdown grid of
    # numbers embeds poorly, and the caption is how a human refers to it.
    text = f"{caption}\n{body}".strip() if caption else body

    return TableElement(
        text=text,
        page_index=page.number,
        page_label=page_label,
        bbox=(bbox.x0, bbox.y0, bbox.x1, bbox.y1),
        markdown=markdown,
        rows=rows,
        caption=caption,
        caption_bbox=tuple(caption_bbox) if caption_bbox is not None else None,
        quality=quality,  # type: ignore[arg-type]
    )


def extract_tables_from_page(
    page: pymupdf.Page,
    page_label: str,
    profile: DocumentProfile | None = None,
) -> list[TableElement]:
    """Every table on one page, each recording which rung of the ladder it
    landed on."""
    found: list[TableElement] = []
    claimed: list[pymupdf.Rect] = []

    # --- rung 1: ruled tables -------------------------------------------
    try:
        located = page.find_tables(strategy="lines").tables
    except Exception:
        located = []

    for table in located:
        bbox = pymupdf.Rect(table.bbox)
        rows = compact(table.extract())
        if not validate(rows):
            continue
        caption_block = _nearest_caption(bbox, table_captions(page, profile))
        found.append(
            _build(
                page,
                page_label,
                bbox,
                caption_block["text"] if caption_block else "",
                rows,
                "ruled",
                caption_block["bbox"] if caption_block else None,
            )
        )
        claimed.append(bbox)

    # --- rungs 2-4: a caption promised a table that rung 1 did not find ---
    for caption_block in table_captions(page, profile):
        if any(_caption_belongs(caption_block["bbox"], c) for c in claimed):
            continue

        # Rung 2: a clip derived from the table's rules. Rung 2b: for a table
        # with no rules at all, the run of narrow blocks below the caption.
        clip = recovery_clip(page, caption_block["bbox"]) or caption_band(
            page, caption_block["bbox"], profile
        )
        if clip is None:
            continue

        rows: list[list[str]] = []
        try:
            recovered = page.find_tables(clip=clip, strategy="text").tables
        except Exception:
            recovered = []
        if recovered:
            rows = compact(max((t.extract() for t in recovered), key=len, default=[]))

        quality = "recovered" if validate(rows) else "text-fallback"
        if quality == "text-fallback":
            rows = []
        found.append(
            _build(
                page,
                page_label,
                clip,
                caption_block["text"],
                rows,
                quality,
                caption_block["bbox"],
            )
        )
        claimed.append(clip)

    found.sort(key=lambda t: t.bbox[1])
    return found


#: A caption may sit this far from its table and still belong to it. Generous
#: because a three-line caption pushes its own box well clear of the rules.
_CAPTION_GAP = 80.0


def _caption_belongs(caption_bbox: pymupdf.Rect, table_bbox: pymupdf.Rect) -> bool:
    """Whether a caption belongs to an already-found table.

    Horizontal overlap is required, then either the boxes intersect or they are
    vertically adjacent. The intersection case is not a curiosity: a detected
    table's bbox starts at its first rule, and a long caption's block can
    extend past that line, so caption and table genuinely overlap. Missing that
    case makes the caption look unclaimed, and the table is then extracted a
    second time from its own caption.
    """
    if not (caption_bbox.x1 > table_bbox.x0 and caption_bbox.x0 < table_bbox.x1):
        return False
    if caption_bbox.intersects(table_bbox):
        return True
    gap = min(abs(caption_bbox.y1 - table_bbox.y0), abs(table_bbox.y1 - caption_bbox.y0))
    return gap < _CAPTION_GAP


def _nearest_caption(bbox: pymupdf.Rect, captions: list[dict]) -> dict | None:
    """The caption for a located table: the closest one that belongs to it.

    Uses the same `_caption_belongs` test as the recovery loop, so a caption is
    never both attached here and treated as unclaimed there.
    """
    owned = [c for c in captions if _caption_belongs(c["bbox"], bbox)]
    if not owned:
        return None
    # Distance between box edges, which is 0 when the caption overlaps the
    # table -- exactly the case a strict above/below split gets wrong.
    return min(
        owned,
        key=lambda c: max(0.0, max(bbox.y0 - c["bbox"].y1, c["bbox"].y0 - bbox.y1)),
    )


__all__ = [
    "compact",
    "extract_tables_from_page",
    "horizontal_rules",
    "recovery_clip",
    "rows_to_markdown",
    "table_captions",
    "validate",
]
