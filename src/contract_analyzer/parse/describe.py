"""Optional: ask Claude what a figure actually shows.

A figure is not text, and the embedder only understands text. The caption alone
answers "which figure is about climate zones?" perfectly well, because that is
how a human refers to a figure. It does not answer "which figure shows the
relationship between airtightness and PM2.5?", because that requires knowing
what the axes are and which way the line goes.

So this is opt-in, behind `--describe-figures`: one small call per figure at
ingest time, storing two or three sentences that get indexed alongside the
caption. Off by default, because it costs money and needs a network, and the
pipeline must stay runnable offline.

Descriptions are written to `FigureElement.description`; the chunker is what
decides to concatenate them into the indexed text.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from ..config import Settings, get_settings
from ..http_client import get_http_client
from ..logger import get_logger
from .elements import FigureElement

log = get_logger(__name__)

#: Claude downsamples images beyond roughly this edge length anyway, and our
#: extracted assets are the publisher's originals -- often 4000px wide for a
#: figure the page shows at 459pt. Sending them untouched wastes bandwidth and
#: tokens for no gain in what the model can see.
MAX_LONG_EDGE = 1568

#: Panels beyond this are dropped from the request; a figure with more than a
#: handful is better summarised from its caption anyway.
MAX_PANELS = 4

SYSTEM_PROMPT = (
    "You describe figures and diagrams from commercial contracts so they can be "
    "found by search. Given a figure and its caption, write two or three "
    "sentences saying what the figure SHOWS: the systems, parties, data flows, "
    "controls or values it depicts, and what a reviewer is meant to conclude. "
    "Do not restate the caption, do not speculate beyond the image, and do not "
    "begin with 'This figure' or 'The image'. Write plain prose, no markdown."
)

_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class DescriptionUnavailable(RuntimeError):
    """No API key, or the `anthropic` package is not installed."""


def _encode(path: Path, max_long_edge: int = MAX_LONG_EDGE) -> dict | None:
    """One image content block, downscaled if it is larger than Claude will use."""
    media_type = _MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        return None

    data = path.read_bytes()
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            if max(image.size) > max_long_edge:
                image.thumbnail((max_long_edge, max_long_edge))
                buffer = io.BytesIO()
                # Flatten to RGB: a palette or alpha image cannot be saved as
                # JPEG, and the alpha channel carries nothing we need.
                image.convert("RGB").save(buffer, format="JPEG", quality=85)
                data, media_type = buffer.getvalue(), "image/jpeg"
    except Exception as exc:  # Pillow missing, or an image it cannot open
        log.debug("not resizing %s: %s", path.name, exc)

    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }


def _client(settings: Settings):
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise DescriptionUnavailable("the `anthropic` package is not installed") from exc

    if not settings.anthropic_key:
        raise DescriptionUnavailable("ANTHROPIC_API_KEY is not set")
    # Through the shared client: one retry policy for every external call.
    return anthropic.Anthropic(
        api_key=settings.anthropic_key, http_client=get_http_client(settings), max_retries=0
    )


def describe_figure(figure: FigureElement, client, model: str) -> str | None:
    """Two or three sentences on what one figure shows, or None if it failed."""
    blocks = [b for b in (_encode(p) for p in figure.asset_paths[:MAX_PANELS]) if b]
    if not blocks:
        return None

    caption = figure.caption or "(this figure has no caption)"
    blocks.append(
        {
            "type": "text",
            "text": (
                f"Caption: {caption}\n"
                f"Section: {figure.breadcrumb or 'unknown'}\n"
                "Describe what this figure shows."
            ),
        }
    )

    response = client.messages.create(
        model=model,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        # A short, self-contained description per figure: low effort is the
        # right setting, and it keeps a 74-figure corpus cheap.
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": blocks}],
    )

    if response.stop_reason == "refusal":
        log.warning("figure on page %s: model declined", figure.page_label)
        return None

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return text or None


def describe_figures(
    figures: list[FigureElement],
    *,
    settings: Settings | None = None,
    model: str | None = None,
    overwrite: bool = False,
) -> int:
    """Fill in `description` on each figure. Returns how many were written.

    A failure on one figure is logged and skipped rather than aborting the
    ingest: a missing description degrades retrieval slightly, while a failed
    ingest leaves the corpus empty.
    """
    settings = settings or get_settings()
    client = _client(settings)
    model = model or settings.answer_model

    written = 0
    for figure in figures:
        if figure.description and not overwrite:
            continue
        try:
            description = describe_figure(figure, client, model)
        except Exception as exc:
            log.warning("figure on page %s: %s", figure.page_label, exc)
            continue
        if description:
            figure.description = description
            written += 1

    return written


__all__ = ["DescriptionUnavailable", "describe_figure", "describe_figures"]
