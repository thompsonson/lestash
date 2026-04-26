"""Pure PDF enrichment orchestrator.

Takes a path to a PDF, returns a structured `EnrichedPdf` artifact. No
database, no network, no media-storage side effects — those happen in the
persistence layer.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from lestash.core.text_extract import convert_to_markdown

from .annotations import extract_annotations
from .cleanup import strip_artifacts
from .images import count_placeholders, extract_images
from .links import apply_links, extract_links
from .types import EnrichedPdf
from .version import EXTRACTOR_VERSION

logger = logging.getLogger(__name__)


def enrich_pdf(pdf_path: Path) -> EnrichedPdf:
    """Run the full enrichment pipeline on a PDF file."""
    import pymupdf

    raw_bytes = pdf_path.read_bytes()
    pdf_sha256 = hashlib.sha256(raw_bytes).hexdigest()

    markdown = convert_to_markdown(pdf_path)

    images: list = []
    annotations: list = []
    links: list = []

    try:
        doc = pymupdf.open(stream=raw_bytes, filetype="pdf")
    except Exception:
        logger.exception("PyMuPDF could not open %s; returning Docling-only output", pdf_path)
        return EnrichedPdf(
            content=strip_artifacts(markdown),
            pdf_sha256=pdf_sha256,
            extractor_version=EXTRACTOR_VERSION,
        )

    try:
        try:
            links = extract_links(doc)
        except Exception:
            logger.exception("Link extraction failed for %s", pdf_path)
        try:
            images = extract_images(doc)
        except Exception:
            logger.exception("Image extraction failed for %s", pdf_path)
        try:
            annotations = extract_annotations(doc)
        except Exception:
            logger.exception("Annotation extraction failed for %s", pdf_path)
    finally:
        doc.close()

    enriched_md = apply_links(markdown, links)
    enriched_md = strip_artifacts(enriched_md)

    placeholder_count = count_placeholders(enriched_md)
    if placeholder_count != len(images):
        logger.warning(
            "Image placeholder count mismatch for %s: %d placeholders vs %d images extracted",
            pdf_path,
            placeholder_count,
            len(images),
        )

    return EnrichedPdf(
        content=enriched_md,
        pdf_sha256=pdf_sha256,
        extractor_version=EXTRACTOR_VERSION,
        images=images,
        annotations=annotations,
    )
