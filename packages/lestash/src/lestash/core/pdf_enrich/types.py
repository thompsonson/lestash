"""Dataclasses for the PDF enrichment pipeline.

These are the public interface between the pure extractor and the persistence
layer. The extractor knows nothing about the database; persistence knows
nothing about PyMuPDF.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

AnnotationKind = Literal["underline", "circle", "margin_note", "ink_unclassified"]


@dataclass
class ExtractedImage:
    placeholder_index: int
    page: int
    bbox: tuple[float, float, float, float]
    bytes_: bytes
    mime_type: str
    xref_hash: str  # sha256 of bytes_, used to dedup repeated images within a doc


@dataclass
class ExtractedAnnotation:
    kind: AnnotationKind
    page: int
    bbox: tuple[float, float, float, float]
    anchor_text: str
    color: str | None
    strokes: list[list[tuple[float, float]]]
    annotation_id: str | None  # PDF /NM (annot.info["id"])
    created_at: str | None  # ISO-8601 from annot.info["creationDate"]
    stroke_geometry_hash: str  # sha256 of canonicalised strokes; OCR cache key


@dataclass
class EnrichedPdf:
    content: str
    pdf_sha256: str
    extractor_version: int
    images: list[ExtractedImage] = field(default_factory=list)
    annotations: list[ExtractedAnnotation] = field(default_factory=list)
