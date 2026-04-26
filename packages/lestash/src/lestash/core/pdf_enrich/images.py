"""Extract embedded images from a PDF.

PyMuPDF returns image refs per page; the same xref may be referenced multiple
times in a doc. We dedup by xref and by content hash (sha256 of bytes) so an
image used as a header on every page becomes one stored asset.

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

logger = logging.getLogger(__name__)

_IMAGE_PLACEHOLDER = re.compile(r"<!-- image -->", re.IGNORECASE)


def extract_images(doc: pymupdf.Document) -> list[ExtractedImage]:
    """Return one ExtractedImage per (page, xref) tuple, deduped by content
    hash within a document."""
    seen_hashes: set[str] = set()
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

            if xref_hash in seen_hashes:
                continue
            seen_hashes.add(xref_hash)

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
