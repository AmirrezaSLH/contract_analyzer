"""One image file as an Anthropic image content block.

Two callers send the same pixels to the same API for different reasons: the
optional ingest-time describer (`describe.py`), which asks Claude what a
figure shows so the words can be indexed, and the query-time Analyzer
(`generation/figures.py`), which attaches a retrieved figure to the request
so the model can read the diagram it is citing. The encoding is identical --
media type, downscale, base64 -- so it lives here and neither imports the
other. Nothing in this module talks to the network.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from ..logger import get_logger

log = get_logger(__name__)

#: Claude downsamples images beyond roughly this edge length anyway, and our
#: extracted assets are the publisher's originals -- often 4000px wide for a
#: figure the page shows at 459pt. Sending them untouched wastes bandwidth and
#: tokens for no gain in what the model can see.
MAX_LONG_EDGE = 1568

#: Panels beyond this are dropped from the request; a figure with more than a
#: handful is better summarised from its caption anyway.
MAX_PANELS = 4

MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def encode_image(path: Path, max_long_edge: int = MAX_LONG_EDGE) -> dict | None:
    """One image content block, downscaled if it is larger than Claude will use.

    `None` -- never an exception -- for a suffix the API does not accept or a
    file that cannot be read: a figure that fails to encode costs the model a
    picture, and must not cost the caller its run.
    """
    media_type = MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        return None

    try:
        data = path.read_bytes()
    except OSError as exc:
        log.warning("figure image unreadable: %s", exc)
        return None
    if not data:
        return None

    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - depends on the environment
        # Nothing here can check the bytes, so they go as the suffix claims.
        log.debug("Pillow absent, sending %s unresized: %s", path.name, exc)
        return _block(media_type, data)

    try:
        with Image.open(io.BytesIO(data)) as image:
            if max(image.size) > max_long_edge:
                image.thumbnail((max_long_edge, max_long_edge))
                buffer = io.BytesIO()
                # Flatten to RGB: a palette or alpha image cannot be saved as
                # JPEG, and the alpha channel carries nothing we need.
                image.convert("RGB").save(buffer, format="JPEG", quality=85)
                data, media_type = buffer.getvalue(), "image/jpeg"
    except Exception as exc:
        # A `.png` Pillow cannot open is not one. Dropping it costs a picture;
        # sending it costs the whole request a 400.
        log.warning("not a readable image, skipping %s: %s", path.name, exc)
        return None

    return _block(media_type, data)


def _block(media_type: str, data: bytes) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }


__all__ = ["MAX_LONG_EDGE", "MAX_PANELS", "MEDIA_TYPES", "encode_image"]
