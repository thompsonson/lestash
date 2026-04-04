"""Convert documents (PDF, DOCX, TXT) to markdown via Docling."""

import logging
from pathlib import Path

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
