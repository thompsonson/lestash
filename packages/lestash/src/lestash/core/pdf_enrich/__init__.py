"""PDF enrichment pipeline. See `docs/pdf-enrichment-design.md`."""

from .extractor import enrich_pdf
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
    "EnrichedPdf",
    "ExtractedAnnotation",
    "ExtractedImage",
    "enrich_pdf",
]
