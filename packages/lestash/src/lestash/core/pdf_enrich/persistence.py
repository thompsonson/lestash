"""Database persistence for the PDF enrichment pipeline.

Owned by the caller (CLI / API), not by the pure extractor. Maps an
`EnrichedPdf` to:

- `item_media` rows for extracted images (media_type='image',
  source_origin='enricher')
- child `items` rows for classified annotations (source_type='pdf_annotation')
- updated parent item with rewritten content + enrichment metadata

Idempotent and re-runnable: prior enricher-derived images and child items are
deleted-then-inserted on every run. The parent's `metadata.pdf_sha256` and
`metadata.extractor_version` track which run produced the current state.

All work happens in a single transaction.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime

from lestash.core.config import Config
from lestash.core.database import (
    add_item_media,
    mark_recent_history,
    max_history_id,
    save_media_file,
)

from .images import apply_images
from .types import EnrichedPdf, ExtractedAnnotation, ExtractedImage

logger = logging.getLogger(__name__)

ANNOTATION_SOURCE_TYPE = "pdf_annotation"
ENRICHER_ORIGIN = "enricher"


def is_already_enriched(
    conn: sqlite3.Connection, item_id: int, current_version: int, current_sha256: str
) -> bool:
    """Return True if the stored (sha256, extractor_version) matches the
    current run — i.e. re-running would be a no-op."""
    row = conn.execute("SELECT metadata FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row or not row[0]:
        return False
    try:
        meta = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return False
    return (
        meta.get("pdf_sha256") == current_sha256
        and meta.get("extractor_version") == current_version
    )


def persist_enrichment(
    conn: sqlite3.Connection,
    config: Config,
    item_id: int,
    enriched: EnrichedPdf,
) -> None:
    """Write the EnrichedPdf to the database in one transaction."""
    try:
        _delete_prior_enricher_artifacts(conn, item_id)
        image_replacements = _store_images(conn, config, item_id, enriched.images)
        rewritten_content = apply_images(enriched.content, image_replacements)
        _store_annotations(conn, item_id, enriched.annotations)
        _update_parent_item(conn, item_id, rewritten_content, enriched, status="ok")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def mark_source_unavailable(conn: sqlite3.Connection, item_id: int) -> None:
    """Flag an item as unrunnable because its source PDF can't be read.

    Leaves existing content intact; only updates the metadata block.
    """
    row = conn.execute("SELECT metadata FROM items WHERE id = ?", (item_id,)).fetchone()
    meta: dict = {}
    if row and row[0]:
        try:
            meta = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            meta = {}
    meta["enrichment_status"] = "source_unavailable"
    meta["enriched_at"] = _now_iso()
    pre_max = max_history_id(conn)
    conn.execute("UPDATE items SET metadata = ? WHERE id = ?", (json.dumps(meta), item_id))
    mark_recent_history(conn, pre_max, "enricher")
    conn.commit()


# --- Internal -----------------------------------------------------------


def _delete_prior_enricher_artifacts(conn: sqlite3.Connection, item_id: int) -> None:
    """Remove enricher-derived media and child items so the new run is clean.

    Does not touch:
    - the source_pdf media row (the original PDF must survive re-runs)
    - non-pdf_annotation children (e.g. unrelated linked items)
    - non-enricher media (uploaded by the user, sync'd from Drive, etc.)
    """
    conn.execute(
        "DELETE FROM item_media WHERE item_id = ? AND source_origin = ?",
        (item_id, ENRICHER_ORIGIN),
    )
    conn.execute(
        "DELETE FROM items WHERE parent_id = ? AND source_type = ?",
        (item_id, ANNOTATION_SOURCE_TYPE),
    )


def _store_images(
    conn: sqlite3.Connection,
    config: Config,
    item_id: int,
    images: list[ExtractedImage],
) -> dict[int, str]:
    """Save image bytes to media storage and create item_media rows.

    Dedups by `xref_hash`: a header image appearing on N pages becomes one
    media row whose media_id is reused for all N placeholder replacements.
    Without this, only the first placeholder would be replaced — see #143.

    Returns a map {placeholder_index: markdown-link} for `apply_images`.
    """
    replacements: dict[int, str] = {}
    hash_to_media_id: dict[str, int] = {}

    for image in images:
        if image.xref_hash in hash_to_media_id:
            media_id = hash_to_media_id[image.xref_hash]
        else:
            ext = _ext_for_mime(image.mime_type)
            filename = f"{image.xref_hash[:12]}{ext}"
            rel_path = save_media_file(item_id, image.bytes_, filename, config=config)
            media_id = add_item_media(
                conn,
                item_id,
                media_type="image",
                local_path=rel_path,
                mime_type=image.mime_type,
                alt_text=f"PDF image (page {image.page + 1})",
                position=image.placeholder_index,
                source_origin=ENRICHER_ORIGIN,
                _commit=False,
            )
            hash_to_media_id[image.xref_hash] = media_id

        replacements[image.placeholder_index] = (
            f"![PDF image (page {image.page + 1})](/api/media/{media_id})"
        )
    return replacements


def _store_annotations(
    conn: sqlite3.Connection,
    parent_id: int,
    annotations: list[ExtractedAnnotation],
) -> None:
    """Insert one child item per semantic annotation."""
    parent_row = conn.execute(
        "SELECT source_type, source_id FROM items WHERE id = ?", (parent_id,)
    ).fetchone()
    if not parent_row:
        return
    parent_source_id = parent_row[1] or str(parent_id)

    for ann in annotations:
        title = _annotation_title(ann)
        content = ann.anchor_text or _human_kind(ann.kind)
        meta = {
            "annotation_kind": ann.kind,
            "page": ann.page,
            "bbox": list(ann.bbox),
            "color": ann.color,
            "strokes": ann.strokes,
            "stroke_geometry_hash": ann.stroke_geometry_hash,
            "annotation_id": ann.annotation_id,
        }
        # Synthesise a stable source_id so re-runs upsert to the same row even
        # though we delete-then-insert above (defensive: stable identity is
        # cheap insurance if delete-then-insert is ever softened).
        source_id = ann.annotation_id or f"{parent_source_id}:{ann.stroke_geometry_hash}"
        conn.execute(
            """
            INSERT INTO items (
                source_type, source_id, title, content,
                created_at, metadata, parent_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ANNOTATION_SOURCE_TYPE,
                source_id,
                title,
                content,
                ann.created_at,
                json.dumps(meta),
                parent_id,
            ),
        )


def _update_parent_item(
    conn: sqlite3.Connection,
    item_id: int,
    content: str,
    enriched: EnrichedPdf,
    *,
    status: str,
) -> None:
    row = conn.execute("SELECT metadata FROM items WHERE id = ?", (item_id,)).fetchone()
    meta: dict = {}
    if row and row[0]:
        try:
            meta = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            meta = {}
    meta["pdf_sha256"] = enriched.pdf_sha256
    meta["extractor_version"] = enriched.extractor_version
    meta["enrichment_status"] = status
    meta["enriched_at"] = _now_iso()
    pre_max = max_history_id(conn)
    conn.execute(
        "UPDATE items SET content = ?, metadata = ? WHERE id = ?",
        (content, json.dumps(meta), item_id),
    )
    mark_recent_history(conn, pre_max, "enricher")


def _annotation_title(ann: ExtractedAnnotation) -> str:
    if ann.anchor_text:
        snippet = ann.anchor_text.strip().splitlines()[0][:60]
        return f"{_human_kind(ann.kind)}: {snippet}"
    return f"{_human_kind(ann.kind)} (page {ann.page + 1})"


def _human_kind(kind: str) -> str:
    return {
        "underline": "Underline",
        "circle": "Circled",
        "margin_note": "Margin note",
        "ink_unclassified": "Ink annotation",
    }.get(kind, kind)


def _ext_for_mime(mime: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/tiff": ".tiff",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    }.get(mime, ".bin")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
