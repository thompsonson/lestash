"""Extract and classify ink annotations from a PDF.

PyMuPDF exposes ink annotations (`/Type /Annot /Subtype /Ink`) with vertices
(stroke point lists) and metadata (color, creation date, NM/UUID).

Classification is pure geometry — no ML, no external deps. Each classifier
returns a confident `kind` or falls through to `ink_unclassified` so we never
drop an annotation. Adding new classifiers later promotes existing
`ink_unclassified` items on the next re-run (handled by ADR D4 versioning).

Tuning constants live at the top of this file. They were chosen as sensible
defaults for Kobo-style strokes and may need adjustment against real samples;
see TUNING.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from typing import TYPE_CHECKING

from .types import AnnotationKind, ExtractedAnnotation

if TYPE_CHECKING:
    import pymupdf

logger = logging.getLogger(__name__)


# --- Tuning constants ---------------------------------------------------
# Underline: y-spread relative to bbox width must be this small.
UNDERLINE_Y_RATIO_MAX = 0.10
UNDERLINE_MIN_LENGTH_PT = 20.0

# Circle: distance from first to last stroke point relative to bbox diagonal.
CIRCLE_CLOSURE_RATIO_MAX = 0.20
CIRCLE_MIN_POINTS = 12

# Margin: bbox center x must lie outside (margin_x_min, margin_x_max) fraction
# of page width. Default: anywhere left of the leftmost 18% or right of the
# rightmost 82% counts as margin.
MARGIN_OUTER_FRACTION = 0.18

# Stroke grouping: two strokes belong to the same annotation if their bbox
# centers are within this many points (PDF "user space" units, ~1pt = 1/72in).
STROKE_GROUPING_DISTANCE_PT = 30.0


def extract_annotations(doc: pymupdf.Document) -> list[ExtractedAnnotation]:
    """Return one ExtractedAnnotation per *semantic* annotation.

    A single circle is typically 5–20 ink strokes; we group spatially-adjacent
    strokes from the same page+annotation source before classifying.
    """
    out: list[ExtractedAnnotation] = []
    for page_num in range(doc.page_count):
        page = doc[page_num]
        try:
            raw_inks = list(_iter_ink_annots(page, page_num))
        except Exception:
            logger.exception("Failed to enumerate annotations on page=%d", page_num)
            continue

        groups = _group_strokes(raw_inks)
        for group in groups:
            try:
                annotation = _classify_group(group, page=page, page_num=page_num)
            except Exception:
                logger.exception("Classifier crashed on page=%d group; falling back", page_num)
                annotation = _fallback_unclassified(group, page=page, page_num=page_num)
            out.append(annotation)
    return out


# --- Internal: raw extraction -------------------------------------------


class _RawInk:
    __slots__ = ("strokes", "bbox", "color", "annotation_id", "created_at", "info")

    def __init__(
        self,
        strokes: list[list[tuple[float, float]]],
        bbox: tuple[float, float, float, float],
        color: str | None,
        annotation_id: str | None,
        created_at: str | None,
    ):
        self.strokes = strokes
        self.bbox = bbox
        self.color = color
        self.annotation_id = annotation_id
        self.created_at = created_at


def _iter_ink_annots(page: pymupdf.Page, page_num: int):
    annots = page.annots() if hasattr(page, "annots") else None
    if annots is None:
        return
    for annot in annots:
        if annot.type[1] != "Ink":
            continue
        vertices = annot.vertices or []
        # PyMuPDF returns a flat list of points; strokes are separated by None
        # in older versions or are nested lists in newer. Handle both.
        strokes = _normalise_vertices(vertices)
        if not strokes:
            continue
        rect = annot.rect
        bbox = (rect.x0, rect.y0, rect.x1, rect.y1)
        info = annot.info or {}
        color = _format_color(annot.colors)
        yield _RawInk(
            strokes=strokes,
            bbox=bbox,
            color=color,
            annotation_id=info.get("id") or None,
            created_at=info.get("creationDate") or None,
        )


def _normalise_vertices(vertices) -> list[list[tuple[float, float]]]:
    """Coerce PyMuPDF's vertex output into a list-of-strokes-of-points."""
    if not vertices:
        return []
    # New form: list of lists of (x, y).
    if (
        isinstance(vertices[0], (list, tuple))
        and vertices[0]
        and isinstance(vertices[0][0], (list, tuple))
    ):
        return [[(float(p[0]), float(p[1])) for p in stroke] for stroke in vertices if stroke]
    # Flat form: list of (x, y) tuples — single stroke.
    return [[(float(p[0]), float(p[1])) for p in vertices]]


def _format_color(colors) -> str | None:
    if not colors:
        return None
    stroke = colors.get("stroke")
    if not stroke:
        return None
    if len(stroke) == 3:
        r, g, b = (int(c * 255) for c in stroke)
        return f"#{r:02x}{g:02x}{b:02x}"
    return None


# --- Internal: stroke grouping ------------------------------------------


def _group_strokes(raw_inks: list[_RawInk]) -> list[list[_RawInk]]:
    """Cluster nearby strokes into one semantic annotation each.

    Many pen apps (Kobo included) emit one Ink annotation per pen-down event,
    so a circle becomes 5–20 distinct annotations. Group them by spatial
    proximity of bbox centers.
    """
    if not raw_inks:
        return []

    centers = [_bbox_center(ink.bbox) for ink in raw_inks]
    n = len(raw_inks)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            if _dist(centers[i], centers[j]) <= STROKE_GROUPING_DISTANCE_PT:
                union(i, j)

    groups: dict[int, list[_RawInk]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(raw_inks[i])
    return list(groups.values())


# --- Internal: classification -------------------------------------------


def _classify_group(
    group: list[_RawInk], *, page: pymupdf.Page, page_num: int
) -> ExtractedAnnotation:
    bbox = _union_bbox([ink.bbox for ink in group])
    all_points = [pt for ink in group for stroke in ink.strokes for pt in stroke]
    all_strokes = [stroke for ink in group for stroke in ink.strokes]

    page_width = page.rect.width
    kind: AnnotationKind = _detect_kind(bbox, all_points, all_strokes, page_width)
    anchor_text = _anchor_text_for(page, bbox, kind, page_width)

    primary = group[0]
    return ExtractedAnnotation(
        kind=kind,
        page=page_num,
        bbox=bbox,
        anchor_text=anchor_text,
        color=primary.color,
        strokes=all_strokes,
        annotation_id=primary.annotation_id,
        created_at=primary.created_at,
        stroke_geometry_hash=_hash_strokes(all_strokes),
    )


def _detect_kind(
    bbox: tuple[float, float, float, float],
    points: list[tuple[float, float]],
    strokes: list[list[tuple[float, float]]],
    page_width: float,
) -> AnnotationKind:
    if not points:
        return "ink_unclassified"

    # Underline: low y-spread relative to width, sufficient length
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    if width >= UNDERLINE_MIN_LENGTH_PT and height <= width * UNDERLINE_Y_RATIO_MAX:
        return "underline"

    # Margin note: bbox center is in the outer band of the page
    cx = (bbox[0] + bbox[2]) / 2.0
    if cx <= page_width * MARGIN_OUTER_FRACTION or cx >= page_width * (1.0 - MARGIN_OUTER_FRACTION):
        return "margin_note"

    # Circle: closed loop, enough points
    if _looks_like_closed_loop(strokes):
        return "circle"

    return "ink_unclassified"


def _looks_like_closed_loop(strokes: list[list[tuple[float, float]]]) -> bool:
    flat = [pt for stroke in strokes for pt in stroke]
    if len(flat) < CIRCLE_MIN_POINTS:
        return False
    start = flat[0]
    end = flat[-1]
    closure = _dist(start, end)
    bbox = _bbox(flat)
    diag = math.hypot(bbox[2] - bbox[0], bbox[3] - bbox[1])
    if diag <= 0:
        return False
    return closure / diag <= CIRCLE_CLOSURE_RATIO_MAX


def _anchor_text_for(
    page: pymupdf.Page,
    bbox: tuple[float, float, float, float],
    kind: AnnotationKind,
    page_width: float,
) -> str:
    """Resolve the text the annotation points at, depending on the kind."""
    import pymupdf as _pm

    if kind == "underline":
        # Text sits *above* the underline stroke; expand bbox upward.
        clip = _pm.Rect(bbox[0], bbox[1] - 14.0, bbox[2], bbox[1] + 2.0)
    elif kind == "circle":
        clip = _pm.Rect(*bbox)
    elif kind == "margin_note":
        # Body text on the same vertical band as the margin scribble.
        cx = (bbox[0] + bbox[2]) / 2.0
        if cx <= page_width / 2.0:
            clip = _pm.Rect(bbox[2], bbox[1], page_width, bbox[3])
        else:
            clip = _pm.Rect(0.0, bbox[1], bbox[0], bbox[3])
    else:
        return ""
    try:
        return page.get_text(clip=clip).strip()
    except Exception:
        return ""


def _fallback_unclassified(group: list[_RawInk], *, page, page_num: int) -> ExtractedAnnotation:
    bbox = _union_bbox([ink.bbox for ink in group])
    all_strokes = [stroke for ink in group for stroke in ink.strokes]
    primary = group[0]
    return ExtractedAnnotation(
        kind="ink_unclassified",
        page=page_num,
        bbox=bbox,
        anchor_text="",
        color=primary.color,
        strokes=all_strokes,
        annotation_id=primary.annotation_id,
        created_at=primary.created_at,
        stroke_geometry_hash=_hash_strokes(all_strokes),
    )


# --- Internal: geometry helpers -----------------------------------------


def _bbox_center(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def _bbox(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _union_bbox(
    boxes: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float]:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _hash_strokes(strokes: list[list[tuple[float, float]]]) -> str:
    """SHA-256 of canonicalised strokes — used as the OCR cache key.

    Coordinates are rounded to 2 decimal places so subpixel jitter from
    repeated extraction doesn't bust the cache.
    """
    canon = [[(round(p[0], 2), round(p[1], 2)) for p in stroke] for stroke in strokes]
    blob = json.dumps(canon, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
