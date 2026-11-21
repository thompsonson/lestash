"""Convert documents (PDF, DOCX, TXT) to markdown via Docling."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from docling_core.types.doc import DoclingDocument

logger = logging.getLogger(__name__)

DOCLING_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/x-msdos-program",  # Drive sometimes reports DOCX as this
}

TEXT_MIME_TYPES = {
    "text/plain",
    "text/markdown",
    "text/csv",
}


def convert_to_markdown(path: Path) -> str:
    """Convert a PDF or DOCX file to markdown using Docling.

    Returns:
        Markdown string, or empty string on failure.
    """
    from docling.document_converter import DocumentConverter

    try:
        converter = DocumentConverter()
        result = converter.convert(str(path))
        return result.document.export_to_markdown()
    except Exception:
        logger.exception("Docling conversion failed for %s", path.name)
        return ""


@dataclass(frozen=True)
class PictureRegion:
    """Where Docling found a picture: a 0-based page and a box in points.

    The box is (x0, y0, x1, y1) measured from the page's top-left corner, as
    PyMuPDF measures it.
    """

    page: int
    bbox: tuple[float, float, float, float]


def convert_pdf(path: Path) -> tuple[str, list[PictureRegion | None]]:
    """Convert a PDF to markdown, with the region of each picture Docling found.

    The i-th region belongs to the i-th `<!-- image -->` placeholder; a picture
    with no known position is None. On failure the markdown is empty.
    """
    from docling.document_converter import DocumentConverter

    try:
        document = DocumentConverter().convert(str(path)).document
        markdown = document.export_to_markdown()
    except Exception:
        logger.exception("Docling conversion failed for %s", path.name)
        return "", []
    try:
        regions = picture_regions(document)
    except Exception:
        logger.exception("Could not read picture positions for %s", path.name)
        regions = []
    return markdown, regions


def picture_regions(document: DoclingDocument) -> list[PictureRegion | None]:
    """The region of each picture, in the order markdown export places them."""
    from docling_core.types.doc import PictureItem

    regions: list[PictureRegion | None] = []
    for item, _level in document.iterate_items():
        if not isinstance(item, PictureItem):
            continue
        page = document.pages.get(item.prov[0].page_no) if item.prov else None
        if page is None:
            regions.append(None)
            continue
        box = item.prov[0].bbox.to_top_left_origin(page_height=page.size.height)
        regions.append(PictureRegion(item.prov[0].page_no - 1, (box.l, box.t, box.r, box.b)))
    return regions


def extract_content(path: Path, mime_type: str) -> str:
    """Extract content from a file as markdown.

    Uses Docling for PDF/DOCX, reads TXT directly.

    Returns:
        Content string, or empty string on failure.
    """
    if mime_type in DOCLING_MIME_TYPES:
        return convert_to_markdown(path)

    if mime_type in TEXT_MIME_TYPES:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            logger.exception("Failed to read text file %s", path.name)
            return ""

    logger.warning("Unsupported mime type %s for %s", mime_type, path.name)
    return ""
