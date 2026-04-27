"""Handwriting OCR via Claude multimodal vision (separate, opt-in pass).

Per ADR 0001 D10: handwritten text in `margin_note` and `ink_unclassified`
child items is not OCR'd locally. We render the stroke geometry to a PNG and
ask Claude to transcribe it.

Idempotent: keyed on (stroke_geometry_hash, ocr_extractor_version). Children
whose stored ocr_version matches the current run are skipped, so re-running
`lestash enrich --ocr` does not re-spend on already-transcribed annotations.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Literal

from lestash.core.config import Config
from lestash.core.database import get_connection, mark_recent_history, max_history_id

OCR_EXTRACTOR_VERSION: int = 1
OCR_TARGET_KINDS = ("margin_note", "ink_unclassified")
ANTHROPIC_MODEL = "claude-sonnet-4-6"
OCR_PROMPT = (
    "Transcribe the handwritten text in this image. "
    "Return only the transcribed text, with no preamble, quotes, or commentary. "
    "If the image contains no legible handwriting, return the single word UNREADABLE."
)
PNG_RENDER_PADDING_PT = 12.0
PNG_RENDER_DPI = 200

logger = logging.getLogger(__name__)

OcrStatus = Literal["transcribed", "skipped", "unavailable", "failed"]


@dataclass
class OcrResult:
    child_id: int
    status: OcrStatus
    text: str | None = None
    message: str | None = None


def ocr_pending_annotations(
    *, item_id: int | None = None, config: Config | None = None
) -> list[OcrResult]:
    """Run OCR on every child annotation that needs it.

    If `item_id` is given, only descendants of that parent are processed.
    Otherwise scans all `pdf_annotation` items in the database.
    """
    config = config or Config.load()
    results: list[OcrResult] = []
    with get_connection(config) as conn:
        candidates = _candidate_children(conn, parent_id=item_id)
    for child_id in candidates:
        results.append(ocr_annotation(child_id, config=config))
    return results


def ocr_annotation(child_id: int, *, config: Config | None = None) -> OcrResult:
    """OCR a single child annotation. Idempotent and side-effect-free if
    `metadata.ocr_version` already matches the current version."""
    config = config or Config.load()
    with get_connection(config) as conn:
        row = conn.execute(
            "SELECT id, parent_id, metadata FROM items WHERE id = ?", (child_id,)
        ).fetchone()
        if not row:
            return OcrResult(child_id=child_id, status="failed", message="not found")
        try:
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
        except (TypeError, json.JSONDecodeError):
            meta = {}

        if meta.get("annotation_kind") not in OCR_TARGET_KINDS:
            return OcrResult(child_id=child_id, status="skipped", message="kind not eligible")
        if meta.get("ocr_version") == OCR_EXTRACTOR_VERSION:
            return OcrResult(child_id=child_id, status="skipped", text=meta.get("ocr_text"))

        try:
            png_bytes = _render_annotation_png(conn, config, row["parent_id"], meta)
        except _OcrUnavailable as exc:
            return OcrResult(child_id=child_id, status="unavailable", message=str(exc))

        try:
            text = _transcribe_with_claude(png_bytes)
        except _OcrUnavailable as exc:
            return OcrResult(child_id=child_id, status="unavailable", message=str(exc))
        except Exception as exc:
            logger.exception("Claude OCR call failed for child %d", child_id)
            return OcrResult(child_id=child_id, status="failed", message=str(exc))

        meta["ocr_text"] = text
        meta["ocr_version"] = OCR_EXTRACTOR_VERSION
        pre_max = max_history_id(conn)
        conn.execute(
            "UPDATE items SET content = ?, metadata = ? WHERE id = ?",
            (text or row["metadata"], json.dumps(meta), child_id),
        )
        mark_recent_history(conn, pre_max, "enricher")
        conn.commit()
        return OcrResult(child_id=child_id, status="transcribed", text=text)


# --- Internal -----------------------------------------------------------


class _OcrUnavailable(Exception):
    """Raised when OCR cannot proceed for environmental reasons (no API key,
    source PDF missing, geometry malformed). Distinct from a model error."""


def _candidate_children(conn: sqlite3.Connection, *, parent_id: int | None) -> list[int]:
    """Return child item IDs that are eligible for OCR but haven't been done
    at the current version."""
    base = """
        SELECT id, metadata FROM items
        WHERE source_type = 'pdf_annotation'
    """
    params: tuple = ()
    if parent_id is not None:
        base += " AND parent_id = ?"
        params = (parent_id,)
    rows = conn.execute(base, params).fetchall()

    out: list[int] = []
    for row in rows:
        try:
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
        except (TypeError, json.JSONDecodeError):
            continue
        if meta.get("annotation_kind") not in OCR_TARGET_KINDS:
            continue
        if meta.get("ocr_version") == OCR_EXTRACTOR_VERSION:
            continue
        out.append(row["id"])
    return out


def _render_annotation_png(
    conn: sqlite3.Connection, config: Config, parent_id: int, meta: dict
) -> bytes:
    """Render the annotation's stroke geometry to a PNG by clipping the
    parent PDF page at the annotation's bbox.

    Strokes themselves are not redrawn — we let PyMuPDF render the page
    region (which already contains the ink as part of the PDF) and let
    Claude see the actual pen marks rather than a synthetic re-render.
    """
    bbox = meta.get("bbox")
    page_num = meta.get("page", 0)
    if not bbox or len(bbox) != 4:
        raise _OcrUnavailable("annotation metadata missing bbox")

    pdf_path = _resolve_parent_pdf(conn, config, parent_id)

    import pymupdf

    try:
        doc = pymupdf.open(pdf_path)
    except Exception as exc:
        raise _OcrUnavailable(f"could not open source PDF: {exc}") from exc
    try:
        if page_num < 0 or page_num >= doc.page_count:
            raise _OcrUnavailable(f"page {page_num} out of range")
        page = doc[page_num]
        clip = pymupdf.Rect(
            max(bbox[0] - PNG_RENDER_PADDING_PT, 0),
            max(bbox[1] - PNG_RENDER_PADDING_PT, 0),
            min(bbox[2] + PNG_RENDER_PADDING_PT, page.rect.width),
            min(bbox[3] + PNG_RENDER_PADDING_PT, page.rect.height),
        )
        zoom = PNG_RENDER_DPI / 72.0
        matrix = pymupdf.Matrix(zoom, zoom)
        pix = page.get_pixmap(clip=clip, matrix=matrix, alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()


def _resolve_parent_pdf(conn: sqlite3.Connection, config: Config, parent_id: int):
    from lestash.core.database import get_media_dir

    row = conn.execute(
        """
        SELECT local_path FROM item_media
        WHERE item_id = ? AND media_type = 'source_pdf'
        ORDER BY id LIMIT 1
        """,
        (parent_id,),
    ).fetchone()
    if not row or not row["local_path"]:
        raise _OcrUnavailable("parent has no source_pdf media row")
    candidate = get_media_dir(config) / row["local_path"]
    if not candidate.exists():
        raise _OcrUnavailable(f"source PDF missing on disk: {candidate}")
    return candidate


def _transcribe_with_claude(png_bytes: bytes) -> str:
    """Send the PNG to Claude and return the transcribed text.

    Raises `_OcrUnavailable` if the SDK is not installed or no API key is
    configured. Other failures bubble up.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise _OcrUnavailable("ANTHROPIC_API_KEY is not set")
    try:
        import anthropic
    except ImportError as exc:
        raise _OcrUnavailable(f"anthropic SDK not installed: {exc}") from exc

    client = anthropic.Anthropic()
    encoded = base64.standard_b64encode(png_bytes).decode("ascii")
    message = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": encoded,
                        },
                    },
                    {"type": "text", "text": OCR_PROMPT},
                ],
            }
        ],
    )

    text_parts: list[str] = []
    for block in message.content:
        # Each content block is a tagged union; only TextBlock has `.text`.
        if getattr(block, "type", None) == "text":
            text_parts.append(getattr(block, "text", ""))
    text = "".join(text_parts).strip()
    if text == "UNREADABLE":
        return ""
    return text
