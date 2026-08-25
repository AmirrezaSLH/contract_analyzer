"""Retrieved figures, sent to the model as pictures.

A figure chunk's indexed text is its caption plus the prose that cites it,
and that is the right thing to embed, to render and to cite -- a reviewer
refers to a figure the way the document does. It is not enough to *read* the
figure: which way the arrow points, what the third column of the diagram
says, whether the flow crosses the processor boundary.

So when a tool result or a chat finisher carries a figure chunk, the image
itself rides along beside the text. This is query time, not ingest time:
only the handful of figures a run actually retrieved are ever encoded, and
nothing is stored. `parse/describe.py` -- the ingest-time describer that
writes a searchable sentence per figure -- stays opt-in and off, and this
module deliberately does not import it: the encoder they share lives in
`parse/images.py`, which talks to no one.

Everything here fails soft. A missing asset, an unreadable file, a format
the API does not take: the image is logged and dropped, the caption still
goes, and the run continues. `send_figure_images: false` in `settings.json`
turns the whole thing off without touching a call site.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT, Settings, get_settings
from ..logger import get_logger
from ..parse.images import MAX_PANELS, encode_image
from ..retrieval import RetrievedChunk

log = get_logger(__name__)

#: Images in one request, across every figure it carries. A ledger full of
#: multi-panel figures would otherwise put dozens of pictures in a single
#: call; the ones after this are dropped, their captions are not.
MAX_IMAGES = 8


def panel_paths(chunk: RetrievedChunk) -> list[Path]:
    """The image files behind one figure chunk, project-root anchored.

    `payload.panels` is the full list the parser grouped into this figure;
    `asset_path` is its first panel and the fallback for a chunk written
    before the payload existed. Capped at `MAX_PANELS`: a figure with more
    than a handful of panels is better read from its caption anyway.
    """
    if chunk.element_type != "figure":
        return []

    panels: list[str] = []
    if chunk.payload:
        try:
            stored = json.loads(chunk.payload).get("panels")
        except (ValueError, AttributeError):
            stored = None
        if isinstance(stored, list):
            panels = [str(p) for p in stored if p]
    if not panels and chunk.asset_path:
        panels = [chunk.asset_path]

    resolved: list[Path] = []
    for panel in panels[:MAX_PANELS]:
        path = Path(panel)
        resolved.append(path if path.is_absolute() else PROJECT_ROOT / path)
    return resolved


def figure_images(
    chunks: Iterable[RetrievedChunk], *, settings: Settings | None = None
) -> dict[int, list[dict[str, Any]]]:
    """`chunk_id` -> its encodable panels, in the order the chunks were given.

    Empty -- never an exception -- when there are no figures, when the files
    are gone, or when the setting is off. A chunk with no readable panel is
    absent rather than present-and-empty, so a caller can name exactly the
    passages whose pictures it is about to send. The same panel reached twice
    is encoded once, and `MAX_IMAGES` caps the request.
    """
    settings = settings or get_settings()
    if not settings.send_figure_images:
        return {}

    images: dict[int, list[dict[str, Any]]] = {}
    seen: set[Path] = set()
    total = 0
    for chunk in chunks:
        for path in panel_paths(chunk):
            if path in seen:
                continue
            seen.add(path)
            if total >= MAX_IMAGES:
                log.info("figure images capped at %d for this request", MAX_IMAGES)
                return images
            if not path.is_file():
                log.warning("figure asset missing, sending caption only: %s", path)
                continue
            block = encode_image(path)
            if block is None:
                log.warning("figure asset not encodable, sending caption only: %s", path)
                continue
            images.setdefault(chunk.chunk_id, []).append(block)
            total += 1
    return images


def figure_blocks(
    chunks: Iterable[RetrievedChunk], *, settings: Settings | None = None
) -> list[dict[str, Any]]:
    """Every image block `figure_images` found, flattened, in chunk order."""
    return [block for blocks in figure_images(chunks, settings=settings).values()
            for block in blocks]


__all__ = ["MAX_IMAGES", "figure_blocks", "figure_images", "panel_paths"]
