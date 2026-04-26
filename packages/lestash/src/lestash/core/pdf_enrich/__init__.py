"""PDF enrichment pipeline. See `docs/pdf-enrichment-design.md`."""

from .extractor import enrich_pdf
from .runner import (
    BackfillStats,
    EnrichmentResult,
    attach_source_pdf_and_enrich,
    backfill_source_pdfs,
    enrich_item,
    list_pdf_items,
)
from .types import (
    AnnotationKind,
    EnrichedPdf,
    ExtractedAnnotation,
    ExtractedImage,
)
from .version import EXTRACTOR_VERSION

__all__ = [
    "AnnotationKind",
    "EXTRACTOR_VERSION",
    "BackfillStats",
    "EnrichedPdf",
    "EnrichmentResult",
    "ExtractedAnnotation",
    "ExtractedImage",
    "attach_source_pdf_and_enrich",
    "backfill_source_pdfs",
    "enrich_item",
    "enrich_pdf",
    "list_pdf_items",
]
