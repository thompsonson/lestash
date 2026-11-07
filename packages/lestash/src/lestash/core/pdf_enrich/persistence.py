"""Database persistence for the PDF enrichment pipeline.

Owned by the caller (CLI / API), not by the pure extractor. Maps an
`EnrichedPdf` to:

- `item_media` rows for extracted images (media_type='image',
  source_origin='enricher')
- child `items` rows for classified annotations (source_type='pdf_annotation')
- updated parent item with rewritten content + enrichment metadata

Idempotent and re-runnable. Child annotation items are upserted by a stable
key (`annotation_id`, else `stroke_geometry_hash`), so they keep their item
IDs and any OCR text across re-runs; only children whose annotation has gone
from the PDF are deleted. Enricher image rows are reused when the same image
file comes back, and dropped when it does not. The parent's
`metadata.pdf_sha256` and `metadata.extractor_version` track which run
produced the current state.

All work happens in a single transaction.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from lestash.core.config import Config
from lestash.core.database import (
    add_item_media,
    get_media_dir,
    mark_recent_history,
    max_history_id,
    save_media_file,
)

from .images import apply_images
from .types import EnrichedPdf, ExtractedAnnotation, ExtractedImage

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

ANNOTATION_SOURCE_TYPE = "pdf_annotation"
ENRICHER_ORIGIN = "enricher"
SOURCE_PDF_MEDIA_TYPE = "source_pdf"
PDF_MIME = "application/pdf"

# Child metadata written by the OCR pass (ocr.py). Kept across re-runs while
# the stroke geometry is unchanged; dropped when it changes so OCR reruns.
OCR_META_KEYS = ("ocr_text", "ocr_version")


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
        image_replacements = _store_images(conn, config, item_id, enriched.images)
        rewritten_content = apply_images(enriched.content, image_replacements)
        _upsert_annotations(conn, item_id, enriched.annotations)
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
    mark_recent_history(conn, pre_max, "enricher", item_ids=[item_id])
    conn.commit()


# --- Internal -----------------------------------------------------------


def _store_images(
    conn: sqlite3.Connection,
    config: Config,
    item_id: int,
    images: list[ExtractedImage],
) -> dict[int, str]:
    """Save image bytes to media storage and upsert item_media rows.

    Dedups by `xref_hash`: a header image appearing on N pages becomes one
    media row whose media_id is reused for all N placeholder replacements.
    Without this, only the first placeholder would be replaced — see #143.

    Media files are named by content hash, so an image that survives a
    re-run maps to the same `local_path`; `add_item_media` then returns the
    existing row and its media_id stays stable. Enricher rows not produced by
    this run are deleted afterwards. Non-enricher media (the source_pdf row,
    user uploads, Drive media) are never touched.

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
            alt_text = f"PDF image (page {image.page + 1})"
            media_id = add_item_media(
                conn,
                item_id,
                media_type="image",
                local_path=rel_path,
                mime_type=image.mime_type,
                alt_text=alt_text,
                position=image.placeholder_index,
                source_origin=ENRICHER_ORIGIN,
                _commit=False,
            )
            # A reused row may carry an older page / position.
            conn.execute(
                "UPDATE item_media SET alt_text = ?, position = ? WHERE id = ?",
                (alt_text, image.placeholder_index, media_id),
            )
            hash_to_media_id[image.xref_hash] = media_id

        replacements[image.placeholder_index] = (
            f"![PDF image (page {image.page + 1})](/api/media/{media_id})"
        )

    _delete_stale_enricher_media(conn, item_id, keep=hash_to_media_id.values())
    return replacements


def _delete_stale_enricher_media(
    conn: sqlite3.Connection, item_id: int, *, keep: Iterable[int]
) -> None:
    keep_ids = list(keep)
    sql = "DELETE FROM item_media WHERE item_id = ? AND source_origin = ?"
    if keep_ids:
        sql += f" AND id NOT IN ({','.join('?' * len(keep_ids))})"
    conn.execute(sql, (item_id, ENRICHER_ORIGIN, *keep_ids))


def _upsert_annotations(
    conn: sqlite3.Connection,
    parent_id: int,
    annotations: list[ExtractedAnnotation],
) -> None:
    """Upsert one child item per semantic annotation (ADR 0001 D6).

    Each annotation is matched to an existing child of this parent by its
    PDF `annotation_id` (/NM), falling back to `stroke_geometry_hash`.

    - Matched, same geometry hash: refresh title/metadata in place; keep the
      child's `content` and OCR metadata if OCR has run on it.
    - Matched, geometry changed (e.g. a stroke was added to the group):
      refresh content and drop the OCR marker so `enrich --ocr` redoes it.
    - Unmatched annotation: insert a new child.
    - Existing child with no annotation left in the PDF: delete it.

    Matching reads the keys from child metadata rather than `source_id`, so
    children written by older runs (whose source_id was the bare /NM) are
    adopted and moved to the parent-scoped source_id.
    """
    parent_row = conn.execute(
        "SELECT source_type, source_id FROM items WHERE id = ?", (parent_id,)
    ).fetchone()
    if not parent_row:
        return
    parent_source_id = parent_row[1] or str(parent_id)

    existing = _load_existing_children(conn, parent_id)
    by_annotation_id: dict[str, list[int]] = {}
    by_hash: dict[str, list[int]] = {}
    for child_id, child_meta in existing.items():
        if child_meta.get("annotation_id"):
            by_annotation_id.setdefault(child_meta["annotation_id"], []).append(child_id)
        if child_meta.get("stroke_geometry_hash"):
            by_hash.setdefault(child_meta["stroke_geometry_hash"], []).append(child_id)

    claimed: set[int] = set()

    def claim(candidates: list[int] | None) -> int | None:
        for child_id in candidates or ():
            if child_id not in claimed:
                claimed.add(child_id)
                return child_id
        return None

    plan: list[tuple[ExtractedAnnotation, int | None]] = []
    for ann in annotations:
        match = claim(by_annotation_id.get(ann.annotation_id)) if ann.annotation_id else None
        if match is None:
            match = claim(by_hash.get(ann.stroke_geometry_hash))
        plan.append((ann, match))

    stale = [child_id for child_id in existing if child_id not in claimed]
    if stale:
        conn.execute(f"DELETE FROM items WHERE id IN ({','.join('?' * len(stale))})", stale)

    # Park matched children on a placeholder source_id first, so reassigning
    # keys between children in one run cannot trip UNIQUE(source_type, source_id).
    for _, match in plan:
        if match is not None:
            conn.execute(
                "UPDATE items SET source_id = ? WHERE id = ?",
                (f"{parent_source_id}:pending:{match}", match),
            )

    pre_max = max_history_id(conn)
    used_keys: set[str] = set()
    touched: list[int] = []
    for ann, match in plan:
        source_id = _unique_source_id(parent_source_id, ann, used_keys)
        title = _annotation_title(ann)
        content: str | None = ann.anchor_text or _human_kind(ann.kind)
        meta: dict[str, Any] = {
            "annotation_kind": ann.kind,
            "page": ann.page,
            "bbox": list(ann.bbox),
            "color": ann.color,
            "strokes": ann.strokes,
            "stroke_geometry_hash": ann.stroke_geometry_hash,
            "annotation_id": ann.annotation_id,
        }

        if match is None:
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
            continue

        old_meta = existing[match]
        if old_meta.get("stroke_geometry_hash") == ann.stroke_geometry_hash:
            for key in OCR_META_KEYS:
                if key in old_meta:
                    meta[key] = old_meta[key]
            if "ocr_version" in old_meta:
                content = None  # COALESCE below keeps the OCR'd content
        conn.execute(
            """
            UPDATE items SET
                source_id = ?,
                title = ?,
                content = COALESCE(?, content),
                created_at = COALESCE(?, created_at),
                metadata = ?
            WHERE id = ?
            """,
            (source_id, title, content, ann.created_at, json.dumps(meta), match),
        )
        touched.append(match)

    mark_recent_history(conn, pre_max, "enricher", item_ids=touched)


def _load_existing_children(conn: sqlite3.Connection, parent_id: int) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, metadata FROM items WHERE parent_id = ? AND source_type = ? ORDER BY id",
        (parent_id, ANNOTATION_SOURCE_TYPE),
    ).fetchall()
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        try:
            meta = json.loads(row[1]) if row[1] else {}
        except (TypeError, json.JSONDecodeError):
            meta = {}
        out[row[0]] = meta if isinstance(meta, dict) else {}
    return out


def _unique_source_id(parent_source_id: str, ann: ExtractedAnnotation, used: set[str]) -> str:
    """Parent-scoped, stable source_id for an annotation child.

    Scoped by the parent because /NM values are only unique within one PDF
    (PyMuPDF, for one, writes `fitz-A0`, `fitz-A1`, ... in every file).
    """
    base = f"{parent_source_id}:{ann.annotation_id or ann.stroke_geometry_hash}"
    source_id = base
    n = 2
    while source_id in used:
        source_id = f"{base}#{n}"
        n += 1
    used.add(source_id)
    return source_id


def set_source_pdf(
    conn: sqlite3.Connection,
    config: Config,
    item_id: int,
    *,
    local_path: str,
    url: str | None = None,
    source_origin: str = "sync",
) -> int:
    """Make `local_path` the item's one current `source_pdf` row.

    - No row yet: insert one.
    - A row exists for another file: update it in place, and append the
      previous version (sha256, local_path, url, replaced_at) to the parent's
      `metadata.source_pdf_versions`. Old files stay on disk; media files are
      content-addressed, so each version keeps its own path.
    - Extra rows left by older code are folded into the history and removed.

    A missing `url` keeps the existing row's url (e.g. a Drive link).
    `source_origin` applies only to a newly inserted row. Does not commit. Returns the media row ID.
    """
    rows = conn.execute(
        "SELECT id, local_path, url FROM item_media WHERE item_id = ? AND media_type = ? "
        "ORDER BY id",
        (item_id, SOURCE_PDF_MEDIA_TYPE),
    ).fetchall()
    if not rows:
        return add_item_media(
            conn,
            item_id,
            media_type=SOURCE_PDF_MEDIA_TYPE,
            url=url,
            local_path=local_path,
            mime_type=PDF_MIME,
            alt_text="original PDF",
            position=0,
            source_origin=source_origin,
            _commit=False,
        )

    keep_id: int = rows[0][0]
    previous = [row for row in rows if row[1] != local_path]
    if previous:
        _record_previous_source_pdfs(conn, config, item_id, previous)
    if len(rows) > 1:
        extra_ids = [row[0] for row in rows[1:]]
        conn.execute(
            f"DELETE FROM item_media WHERE id IN ({','.join('?' * len(extra_ids))})",
            extra_ids,
        )
    conn.execute(
        """
        UPDATE item_media SET local_path = ?, url = COALESCE(?, url), mime_type = ?,
            alt_text = 'original PDF', position = 0
        WHERE id = ?
        """,
        (local_path, url, PDF_MIME, keep_id),
    )
    return keep_id


def _record_previous_source_pdfs(
    conn: sqlite3.Connection,
    config: Config,
    item_id: int,
    rows: list[sqlite3.Row],
) -> None:
    import hashlib

    meta = _load_item_metadata(conn, item_id)
    versions = meta.get("source_pdf_versions")
    if not isinstance(versions, list):
        versions = []
    known = {v.get("local_path") for v in versions if isinstance(v, dict)}
    media_dir = get_media_dir(config)
    replaced_at = _now_iso()
    for row in rows:
        old_path = row[1]
        if old_path in known:
            continue
        sha256 = None
        if old_path:
            candidate = media_dir / old_path
            if candidate.exists():
                sha256 = hashlib.sha256(candidate.read_bytes()).hexdigest()
        versions.append(
            {
                "sha256": sha256,
                "local_path": old_path,
                "url": row[2],
                "replaced_at": replaced_at,
            }
        )
        known.add(old_path)
    meta["source_pdf_versions"] = versions
    pre_max = max_history_id(conn)
    conn.execute("UPDATE items SET metadata = ? WHERE id = ?", (json.dumps(meta), item_id))
    mark_recent_history(conn, pre_max, "enricher", item_ids=[item_id])


def _load_item_metadata(conn: sqlite3.Connection, item_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT metadata FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row or not row[0]:
        return {}
    try:
        meta = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return {}
    return meta if isinstance(meta, dict) else {}


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
    mark_recent_history(conn, pre_max, "enricher", item_ids=[item_id])


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
