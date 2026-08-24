"""Figure extraction: raster assets, vector fallback, and caption pairing.

A figure is not text, and the embedder only understands text, so what actually
gets indexed is the caption -- optionally joined at ingest by a generated
description. The image itself is written to disk and referenced by path, which
keeps `rag.db` small and the files inspectable.

Two cases have to be handled. Most figures are embedded raster images and are
simply extracted at their native resolution (often 4000px wide for a figure the
page shows at 459pt). A few are drawn as vector paths and have no image to
extract at all; those pages are detected by a `Figure N.M:` caption with no
raster to pair it with, and the region is rendered instead.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from .blocks import CAPTION_RE, DocumentProfile, block_text, text_blocks
from .elements import FigureElement

#: Below this, an image is a rule, a bullet glyph or a logo rather than a
#: figure. Both bounds must be cleared: native pixels *and* the share of the
#: page it is drawn across.
MIN_PIXELS = 100
MIN_PAGE_AREA_SHARE = 0.01

#: Rendering resolution for figures that exist only as vector drawing operators.
VECTOR_RENDER_DPI = 150

#: Two panels of the same figure sit within this many points of each other
#: vertically. Wider apart and they are separate figures that happen to share a
#: page.
PANEL_GAP = 90.0

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _slug(text: str) -> str:
    return _UNSAFE.sub("_", text).strip("_") or "document"


def asset_dir(assets_dir: Path, document_id: str) -> Path:
    """Where one document's figures live: `data/assets/<slug>/`.

    Public because ingestion needs it too -- re-ingesting a changed file clears
    this directory first, and it must clear the same one the extractor writes.
    """
    return Path(assets_dir) / _slug(document_id)


@dataclass
class _Caption:
    """A caption block on a page, with the geometry needed to pair it."""

    text: str
    y0: float
    y1: float
    kind: str  # "figure" or "table"
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


def page_captions(page: pymupdf.Page, profile: DocumentProfile | None = None) -> list[_Caption]:
    """Every `Figure N.M:` / `Table N.M:` caption on a page, top to bottom."""
    captions: list[_Caption] = []
    for block in text_blocks(page):
        text = block_text(block, profile)
        match = CAPTION_RE.match(text)
        if match:
            captions.append(
                _Caption(
                    text=text,
                    y0=block["bbox"][1],
                    y1=block["bbox"][3],
                    kind=match.group(1).lower(),
                    bbox=tuple(block["bbox"]),
                )
            )
    return captions


def pair_caption(rect: pymupdf.Rect, captions: list[_Caption], kind: str) -> _Caption | None:
    """The caption belonging to a region.

    LaTeX sets figure captions below the float and table captions above it, so
    the preferred direction depends on `kind`; the other direction is tried as
    a fallback. Nearest wins. Measured correct on 6 of 6 spot checks.
    """
    same_kind = [c for c in captions if c.kind == kind]
    below = sorted((c for c in same_kind if c.y0 >= rect.y1 - 2), key=lambda c: c.y0 - rect.y1)
    above = sorted((c for c in same_kind if c.y1 <= rect.y0 + 2), key=lambda c: rect.y0 - c.y1)

    order = (below, above) if kind == "figure" else (above, below)
    for group in order:
        if group:
            return group[0]
    return None


@dataclass
class FigureExtractor:
    """Per-document figure extraction state.

    Held across pages so a logo repeated on every page is written once: assets
    are de-duplicated on the SHA-256 of their bytes.
    """

    doc: pymupdf.Document
    assets_dir: Path
    document_id: str
    #: Supplies the vocabulary used to de-hyphenate caption text.
    profile: DocumentProfile | None = None
    #: sha256 -> path already written.
    _seen: dict[str, Path] = field(default_factory=dict)

    @property
    def target_dir(self) -> Path:
        return asset_dir(self.assets_dir, self.document_id)

    def _write(self, data: bytes, name: str) -> Path:
        digest = hashlib.sha256(data).hexdigest()
        if digest in self._seen:
            return self._seen[digest]

        self.target_dir.mkdir(parents=True, exist_ok=True)
        path = self.target_dir / name
        path.write_bytes(data)
        self._seen[digest] = path
        return path

    def page_figures(
        self,
        page: pymupdf.Page,
        page_label: str,
        claimed: list[pymupdf.Rect] | None = None,
    ) -> list[FigureElement]:
        """Every figure on one page, panels already grouped."""
        claimed = claimed or []
        captions = page_captions(page, self.profile)
        page_area = abs(page.rect) or 1.0

        # --- raster images -------------------------------------------------
        panels: list[tuple[pymupdf.Rect, Path, int, int, _Caption | None]] = []
        for info in page.get_images(full=True):
            xref = info[0]
            try:
                image = self.doc.extract_image(xref)
            except Exception:
                continue  # a broken or unsupported XObject is not worth failing on
            if not image or image["width"] < MIN_PIXELS or image["height"] < MIN_PIXELS:
                continue

            for index, rect in enumerate(page.get_image_rects(xref)):
                if abs(rect) / page_area < MIN_PAGE_AREA_SHARE:
                    continue
                if any(rect.intersects(region) for region in claimed):
                    continue
                suffix = "" if index == 0 else f"_{index}"
                path = self._write(
                    image["image"],
                    f"p{page.number:03d}_{xref}{suffix}.{image['ext']}",
                )
                caption = pair_caption(rect, captions, "figure")
                panels.append((rect, path, image["width"], image["height"], caption))

        figures = self._group_panels(panels, page, page_label)

        # --- vector figures ------------------------------------------------
        # A figure caption with no raster to pair it with means the artwork was
        # drawn rather than embedded.
        paired = {figure.caption for figure in figures if figure.caption}
        for caption in captions:
            if caption.kind != "figure" or caption.text in paired:
                continue
            vector = self._render_vector(page, page_label, caption, claimed)
            if vector is not None:
                figures.append(vector)

        figures.sort(key=lambda f: f.bbox[1])
        return figures

    def _group_panels(
        self,
        panels: list[tuple[pymupdf.Rect, Path, int, int, _Caption | None]],
        page: pymupdf.Page,
        page_label: str,
    ) -> list[FigureElement]:
        """Collapse the panels of a multi-panel figure into one element.

        Our corpus embeds 74 images against 53 captions because a figure with
        several panels stores one image per panel. Panels are grouped when they
        share a caption *and* are vertically adjacent -- the second condition
        stops two unrelated figures that happen to have lost their captions
        from being welded together.
        """
        groups: list[list[tuple[pymupdf.Rect, Path, int, int, _Caption | None]]] = []
        for panel in sorted(panels, key=lambda p: (p[0].y0, p[0].x0)):
            rect, _, _, _, caption = panel
            if groups:
                prev_rect, _, _, _, prev_caption = groups[-1][-1]
                same_caption = (
                    caption is not None
                    and prev_caption is not None
                    and caption.text == prev_caption.text
                )
                if same_caption and rect.y0 - prev_rect.y1 <= PANEL_GAP:
                    groups[-1].append(panel)
                    continue
            groups.append([panel])

        figures: list[FigureElement] = []
        for group in groups:
            bbox = pymupdf.Rect(group[0][0])
            for rect, *_ in group[1:]:
                bbox |= rect
            caption = group[0][4]
            text = caption.text if caption else f"Figure on page {page_label}"
            figures.append(
                FigureElement(
                    text=text,
                    page_index=page.number,
                    page_label=page_label,
                    bbox=(bbox.x0, bbox.y0, bbox.x1, bbox.y1),
                    asset_paths=[path for _, path, _, _, _ in group],
                    caption=caption.text if caption else "",
                    caption_bbox=caption.bbox if caption else None,
                    width=max(w for _, _, w, _, _ in group),
                    height=max(h for _, _, _, h, _ in group),
                )
            )
        return figures

    def _render_vector(
        self,
        page: pymupdf.Page,
        page_label: str,
        caption: _Caption,
        claimed: list[pymupdf.Rect],
    ) -> FigureElement | None:
        """Render a figure that exists only as drawing operators.

        There is no image to extract, so the region above the caption is
        rasterised instead. Same output format and same downstream handling as
        an embedded image.
        """
        band = pymupdf.Rect(page.rect.x0, page.rect.y0, page.rect.x1, caption.y0)
        cluster = pymupdf.Rect()
        for drawing in page.get_drawings():
            rect = drawing["rect"]
            # Ignore hairlines: those are table rules and underlines, not art.
            if rect.height < 2 and rect.width < 2:
                continue
            if rect.intersects(band):
                cluster |= rect

        if cluster.is_empty or abs(cluster) / (abs(page.rect) or 1.0) < MIN_PAGE_AREA_SHARE:
            return None
        if any(cluster.intersects(region) for region in claimed):
            return None

        cluster = cluster + (-4, -4, 4, 4)  # a little air around the drawing
        pixmap = page.get_pixmap(dpi=VECTOR_RENDER_DPI, clip=cluster & page.rect)
        path = self._write(pixmap.tobytes("png"), f"p{page.number:03d}_vector{int(caption.y0)}.png")

        return FigureElement(
            text=caption.text,
            page_index=page.number,
            page_label=page_label,
            bbox=(cluster.x0, cluster.y0, cluster.x1, cluster.y1),
            asset_paths=[path],
            caption=caption.text,
            caption_bbox=caption.bbox,
            width=pixmap.width,
            height=pixmap.height,
        )


__all__ = ["FigureExtractor", "asset_dir", "page_captions", "pair_caption"]
