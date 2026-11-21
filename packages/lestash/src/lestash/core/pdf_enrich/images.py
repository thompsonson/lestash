"""Extract embedded images from a PDF.

PyMuPDF returns image refs per page; the same xref may be referenced multiple
times in a doc. We emit **one ExtractedImage per page-occurrence** so each
Docling `<!-- image -->` placeholder gets replaced — but the bytes are the
same across occurrences (carry the same `xref_hash`) so the persistence layer
can dedup the on-disk media row and reuse a single media_id for all copies.

Images are matched to Docling's `<!-- image -->` placeholders later in
`apply_images`. The matching is by reading order — Docling emits placeholders
in document order, and PyMuPDF's `page.get_images()` is also in page order.
This is best-effort; mis-matches are logged but never crash.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import TYPE_CHECKING

from .types import ExtractedImage

if TYPE_CHECKING:
    import pymupdf

    from lestash.core.text_extract import PictureRegion

logger = logging.getLogger(__name__)

_IMAGE_PLACEHOLDER = re.compile(r"<!-- image -->", re.IGNORECASE)


def extract_images(doc: pymupdf.Document) -> list[ExtractedImage]:
    """Return one ExtractedImage per page-occurrence.

    Repeated xrefs (e.g. a header image on every page) appear once per page
    and carry the same `xref_hash`. Persistence dedups by hash and shares
    the resulting media row across placeholder substitutions.
    """
    placeholder_idx = 0
    images: list[ExtractedImage] = []

    for page_num in range(doc.page_count):
        page = doc[page_num]
        page_images = page.get_images(full=True)
        for img_info in page_images:
            xref = img_info[0]
            try:
                extracted = doc.extract_image(xref)
            except Exception:
                logger.exception("Failed to extract image xref=%s on page=%d", xref, page_num)
                continue

            data: bytes = extracted["image"]
            ext: str = extracted.get("ext", "png")
            xref_hash = hashlib.sha256(data).hexdigest()

            try:
                rects = page.get_image_rects(xref)
                rect = rects[0] if rects else None
            except Exception:
                rect = None
            if rect is not None:
                bbox = (rect.x0, rect.y0, rect.x1, rect.y1)
            else:
                bbox = (0.0, 0.0, 0.0, 0.0)

            mime = _mime_for_ext(ext)
            images.append(
                ExtractedImage(
                    placeholder_index=placeholder_idx,
                    page=page_num,
                    bbox=bbox,
                    bytes_=data,
                    mime_type=mime,
                    xref_hash=xref_hash,
                )
            )
            placeholder_idx += 1

    return images


# A picture is rendered at twice PDF resolution, so a diagram's small text stays legible
RENDER_DPI = 144

# An image inside a picture raises the resolution to its own, up to this
MAX_RENDER_DPI = 300

# An image is part of a picture when this share of its part on the page lies inside
# the region, and it fills at least PHOTO_SHARE of the region
PHOTO_INSIDE = 0.5
PHOTO_SHARE = 0.1

# A picture is a JPEG when images stored as JPEG cover this share of it
JPEG_SHARE = 0.5


def extract_pictures(
    doc: pymupdf.Document, regions: list[PictureRegion | None]
) -> list[ExtractedImage]:
    """Return one ExtractedImage per picture Docling found, by placeholder index.

    Each picture is rendered from its region of the page, so everything in it is
    kept: shapes, text and any images (#391). A picture holding an image is
    rendered at the image's own resolution. It is a JPEG only when images stored
    as JPEG cover most of it, so a diagram, screenshot or chart stays a PNG.
    """
    images: list[ExtractedImage] = []
    blocks_by_page: dict[int, list[dict]] = {}
    for index, region in enumerate(regions):
        clip = _clip(doc, region)
        if region is None or clip is None:
            continue
        page = doc[region.page]
        if region.page not in blocks_by_page:
            blocks_by_page[region.page] = _image_blocks(page)
        image_dpi, jpeg_area = _images_in(blocks_by_page[region.page], page, clip)
        dpi = min(max(RENDER_DPI, image_dpi), MAX_RENDER_DPI)
        ext = "jpeg" if jpeg_area >= JPEG_SHARE * clip.get_area() else "png"
        data = page.get_pixmap(clip=clip, dpi=round(dpi)).tobytes(ext)
        images.append(
            ExtractedImage(
                placeholder_index=index,
                page=region.page,
                bbox=(clip.x0, clip.y0, clip.x1, clip.y1),
                bytes_=data,
                mime_type=_mime_for_ext(ext),
                xref_hash=hashlib.sha256(data).hexdigest(),
            )
        )
    return images


def _image_blocks(page: pymupdf.Page) -> list[dict]:
    """Every image drawn on the page, inline ones included, with its stored format."""
    try:
        return [b for b in page.get_text("dict")["blocks"] if b.get("type") == 1]
    except Exception:
        logger.exception("Could not list the images on page %d", page.number)
        return []


def _images_in(blocks: list[dict], page: pymupdf.Page, clip: pymupdf.Rect) -> tuple[float, float]:
    """The highest resolution of the images that are part of `clip`, and the area
    of `clip` that images stored as JPEG cover.

    An image is part of the picture when most of its part on the page lies inside
    the region and it fills a fair share of it, so a page background or a small
    icon doesn't count. An image cropped by a clipping path is measured by its
    whole placement, which PyMuPDF reports; see #393.
    """
    import math

    import pymupdf

    best, jpeg_area = 0.0, 0.0
    for block in blocks:
        shown = pymupdf.Rect(block["bbox"]) & page.rect
        inside = (shown & clip).get_area()
        if shown.is_empty or inside < PHOTO_INSIDE * shown.get_area():
            continue
        if inside < PHOTO_SHARE * clip.get_area():
            continue
        # The matrix maps the image's unit square onto the page, so each of its
        # axes' length in points holds, whatever the rotation or scaling
        matrix = pymupdf.Matrix(block["transform"])
        x_points, y_points = math.hypot(matrix.a, matrix.b), math.hypot(matrix.c, matrix.d)
        if x_points == 0 or y_points == 0:
            continue
        best = max(best, max(block["width"] / x_points, block["height"] / y_points) * 72)
        if block.get("ext") in ("jpeg", "jpx"):
            jpeg_area += inside
    return best, jpeg_area


def _clip(doc: pymupdf.Document, region: PictureRegion | None) -> pymupdf.Rect | None:
    """The region's box on its page, or None if it has no area there."""
    import pymupdf

    if region is None or not 0 <= region.page < doc.page_count:
        return None
    clip = pymupdf.Rect(region.bbox) & doc[region.page].rect
    return None if clip.is_empty else clip


def regions_fit(
    doc: pymupdf.Document, regions: list[PictureRegion | None], placeholders: int
) -> bool:
    """True if every placeholder has a region with some area on a page of this document."""
    return len(regions) == placeholders > 0 and all(_clip(doc, r) is not None for r in regions)


def apply_images(markdown: str, replacements: dict[int, str]) -> str:
    """Replace `<!-- image -->` placeholders in document order.

    `replacements[i]` is the markdown to substitute for the i-th placeholder
    (typically `![alt](/api/media/{id})`). Placeholders without a replacement
    are left intact (logged as warnings by the caller).
    """
    counter = -1

    def _sub(match: re.Match[str]) -> str:
        nonlocal counter
        counter += 1
        return replacements.get(counter, match.group(0))

    return _IMAGE_PLACEHOLDER.sub(_sub, markdown)


def count_placeholders(markdown: str) -> int:
    return len(_IMAGE_PLACEHOLDER.findall(markdown))


def _mime_for_ext(ext: str) -> str:
    ext = ext.lower().lstrip(".")
    return {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "tiff": "image/tiff",
        "tif": "image/tiff",
        "webp": "image/webp",
        "bmp": "image/bmp",
    }.get(ext, f"image/{ext}")
