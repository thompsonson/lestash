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

# A raster image is the picture when each covers this share of the other: the image
# fills the region, and is no larger, so a page background never stands in for a diagram
RASTER_COVER = 0.8


def extract_pictures(
    doc: pymupdf.Document, regions: list[PictureRegion | None]
) -> list[ExtractedImage]:
    """Return one ExtractedImage per picture Docling found, by placeholder index.

    A raster image that fills the picture's region is kept as stored. Anything
    else, such as a diagram drawn as vector shapes, is rendered from the page,
    so it is saved as a picture too (#391).
    """
    images: list[ExtractedImage] = []
    for index, region in enumerate(regions):
        clip = _clip(doc, region)
        if region is None or clip is None:
            continue
        page = doc[region.page]
        stored = _raster_filling(doc, page, clip) if _only_an_image(page, clip) else None
        if stored is not None:
            data, ext = stored
        else:
            data, ext = page.get_pixmap(clip=clip, dpi=RENDER_DPI).tobytes("png"), "png"
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


def _raster_filling(
    doc: pymupdf.Document, page: pymupdf.Page, clip: pymupdf.Rect
) -> tuple[bytes, str] | None:
    """The stored bytes and extension of a raster image that fills `clip`, if any."""
    for info in page.get_images(full=True):
        xref = info[0]
        try:
            rects = page.get_image_rects(xref)
        except Exception:
            continue
        if any(_same_place(rect, clip) for rect in rects):
            try:
                extracted = doc.extract_image(xref)
            except Exception:
                logger.exception("Failed to extract image xref=%s", xref)
                return None
            return extracted["image"], extracted.get("ext", "png")
    return None


def _clip(doc: pymupdf.Document, region: PictureRegion | None) -> pymupdf.Rect | None:
    """The region's box on its page, or None if it has no area there."""
    import pymupdf

    if region is None or not 0 <= region.page < doc.page_count:
        return None
    clip = pymupdf.Rect(region.bbox) & doc[region.page].rect
    return None if clip.is_empty else clip


def _only_an_image(page: pymupdf.Page, clip: pymupdf.Rect) -> bool:
    """True if no text or drawing lies in the region, so a stored image shows all of it.

    Labels or arrows drawn over a photo would be lost from the photo's own bytes.
    """
    if page.get_text("words", clip=clip):
        return False
    # Compared edge by edge: a straight line's box has no area, so `&` would miss it
    return not any(
        r.x0 <= clip.x1 and r.x1 >= clip.x0 and r.y0 <= clip.y1 and r.y1 >= clip.y0
        for r in (d["rect"] for d in page.get_drawings())
    )


def _same_place(rect: pymupdf.Rect, clip: pymupdf.Rect) -> bool:
    overlap = (rect & clip).get_area()
    return overlap >= RASTER_COVER * clip.get_area() and overlap >= RASTER_COVER * rect.get_area()


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
