"""Convert documents (PDF, DOCX, TXT) to markdown via Docling."""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from docling_core.types.doc import DoclingDocument
    from PIL.Image import Image as PILImage

logger = logging.getLogger(__name__)

DOCX_MIME_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/x-msdos-program",  # Drive sometimes reports DOCX as this
}

DOCLING_MIME_TYPES = {"application/pdf", *DOCX_MIME_TYPES}

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


@dataclass(frozen=True)
class DocPicture:
    """A picture's file, as the document holds it where it can (#394)."""

    data: bytes
    mime_type: str


# Picture files a browser shows; any other kind (EMF, TIFF...) is saved as Docling's PNG
_WEB_IMAGE_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}


def convert_docx(path: Path) -> tuple[str, list[DocPicture | None]]:
    """Convert a Word file to markdown, with the file of each picture Docling found.

    The i-th picture belongs to the i-th `<!-- image -->` placeholder; one Docling
    could not read is None. On failure the markdown is empty.
    """
    from docling.document_converter import DocumentConverter

    try:
        document = DocumentConverter().convert(str(path)).document
        markdown = document.export_to_markdown()
    except Exception:
        logger.exception("Docling conversion failed for %s", path.name)
        return "", []
    try:
        pictures = docx_pictures(path, document)
    except Exception:
        logger.exception("Could not read the pictures in %s", path.name)
        pictures = []
    return markdown, pictures


def docx_pictures(path: Path, document: DoclingDocument) -> list[DocPicture | None]:
    """Each picture's file, in the order markdown export places them.

    Docling keeps each picture as a PNG it re-encoded. Where those pixels match a
    file in the Word file, that original is kept instead, so a photo stays a JPEG.
    """
    from docling_core.types.doc import PictureItem

    originals = _docx_originals(path)
    pictures: list[DocPicture | None] = []
    for item, _level in document.iterate_items():
        if not isinstance(item, PictureItem):
            continue
        image = item.image.pil_image if item.image else None
        if image is None:
            pictures.append(None)
            continue
        original = originals.get(_pixels_key(image))
        if original is not None:
            pictures.append(original)
            continue
        uri = str(item.image.uri) if item.image else ""
        if uri.startswith("data:image/png;base64,"):
            data = base64.b64decode(uri.split(",", 1)[1])
        else:
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            data = buffer.getvalue()
        pictures.append(DocPicture(data, "image/png"))
    return pictures


def _docx_originals(path: Path) -> dict[tuple[str, tuple[int, int], str], DocPicture]:
    """The Word file's own picture files a browser can show, by their pixels."""
    from PIL import Image, UnidentifiedImageError

    originals: dict[tuple[str, tuple[int, int], str], DocPicture] = {}
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not name.startswith("word/media/"):
                continue
            data = archive.read(name)
            try:
                with Image.open(io.BytesIO(data)) as image:
                    mime_type = _WEB_IMAGE_TYPES.get(image.format or "")
                    if mime_type is None:
                        continue
                    image.load()
                    originals.setdefault(_pixels_key(image), DocPicture(data, mime_type))
            except (UnidentifiedImageError, OSError, ValueError):
                continue
    return originals


def _pixels_key(image: PILImage) -> tuple[str, tuple[int, int], str]:
    return image.mode, image.size, hashlib.sha256(image.tobytes()).hexdigest()


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
