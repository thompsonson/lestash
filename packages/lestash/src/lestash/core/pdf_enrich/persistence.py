"""Database persistence for the PDF enrichment pipeline.

Owned by the caller (CLI / API), not by the pure extractor. Maps an
`EnrichedPdf` to:

- `item_media` rows for extracted images (media_type='image',
  source_origin='enricher')
- child `items` rows for classified annotations (source_type='pdf_annotation')
- updated parent item with rewritten content + enrichment metadata

Idempotent and re-runnable. Child annotation items are matched to the
annotations of a re-run (same strokes on the same page, else the same /NM on
the same page with overlapping boxes, else unnamed ink by place), so they keep
their item IDs and any OCR text. A child whose annotation has left the PDF is
kept and marked `removed_from_pdf`, not deleted. Enricher image rows are reused
when the same image file comes back, and dropped when it does not. The parent's
`metadata.pdf_sha256` and `metadata.extractor_version` track which run
produced the current state.

All work happens in a single transaction.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NamedTuple

from lestash.core.blobs import media_file
from lestash.core.config import Config
from lestash.core.database import (
    add_item_media,
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
# A source PDF replaced by a newer file, kept for history (see set_source_pdf)
SOURCE_PDF_VERSION_MEDIA_TYPE = "source_pdf_version"
PDF_MIME = "application/pdf"

# Child metadata written by the OCR pass (ocr.py). Kept across re-runs while
# the stroke geometry is unchanged; dropped when it changes so OCR reruns.
OCR_META_KEYS = ("ocr_text", "ocr_version")
# Set on a child whose annotation has left the PDF (see _upsert_annotations)
REMOVED_KEY = "removed_from_pdf"
REMOVED_TITLE_PREFIX = "[Removed from PDF] "
# Annotation kinds the OCR pass transcribes (ocr.py re-exports this)
OCR_TARGET_KINDS = ("margin_note", "ink_unclassified")


def is_already_enriched(
    conn: sqlite3.Connection, item_id: int, current_version: int, current_sha256: str
) -> bool:
    """Return True if the stored (sha256, extractor_version) matches the
    current run — i.e. re-running would be a no-op."""
    meta = _load_item_metadata(conn, item_id)
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
    meta = _load_item_metadata(conn, item_id)
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
            # A reused row may carry an older page / position. Only the
            # enricher's own rows: a user's image with the same bytes is left alone
            conn.execute(
                "UPDATE item_media SET alt_text = ?, position = ? "
                "WHERE id = ? AND source_origin = ?",
                (alt_text, image.placeholder_index, media_id, ENRICHER_ORIGIN),
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
    # Never the source PDF or its versions, whatever origin wrote them
    sql = (
        "DELETE FROM item_media WHERE item_id = ? AND source_origin = ?"
        " AND media_type NOT IN (?, ?)"
    )
    if keep_ids:
        sql += f" AND id NOT IN ({','.join('?' * len(keep_ids))})"
    conn.execute(
        sql,
        (
            item_id,
            ENRICHER_ORIGIN,
            SOURCE_PDF_MEDIA_TYPE,
            SOURCE_PDF_VERSION_MEDIA_TYPE,
            *keep_ids,
        ),
    )


def _upsert_annotations(
    conn: sqlite3.Connection,
    parent_id: int,
    annotations: list[ExtractedAnnotation],
) -> None:
    """Upsert one child item per semantic annotation (ADR 0001 D6).

    Each annotation is matched to an existing child of this parent, in three
    passes. Live children are tried before ones already marked removed.

    1. The same strokes on the same page (`page`, `stroke_geometry_hash`).
    2. Otherwise a shared stroke `/NM` on the same page, with overlapping boxes:
       the same annotation with a stroke added or erased. Any member's name
       counts, since erasing the first stroke changes the group's
       `annotation_id`. A name alone is not enough: PyMuPDF names restart on
       each page and are reused after a deletion.
    3. Otherwise, for unnamed ink (the writer left `/NM` out), an unnamed child
       on the same page whose box overlaps.

    - Matched, same strokes and still an OCR kind: refresh title/metadata in
      place; keep the child's `content` and OCR metadata if OCR has run on it.
    - Matched, strokes changed: refresh content and drop the OCR marker so
      `enrich --ocr` redoes it.
    - Unmatched annotation: insert a new child.
    - Existing child with no annotation left in the PDF: keep it, marked
      `removed_from_pdf`; a later match restores it.
    - A child whose row would not change is not written.

    Matching reads the keys from child metadata rather than `source_id`, so
    children written by older runs (whose source_id was the bare /NM) are
    adopted and moved to the current source_id.
    """
    if not conn.execute("SELECT 1 FROM items WHERE id = ?", (parent_id,)).fetchone():
        return
    parent_key = f"item-{parent_id}"

    existing = _load_existing_children(conn, parent_id)
    # Live children first, so a removed child never takes a live one's annotation
    order = sorted(existing, key=lambda cid: (bool(existing[cid].meta.get(REMOVED_KEY)), cid))
    by_geometry: dict[tuple[Any, str], list[int]] = {}
    by_name: dict[tuple[Any, str], list[int]] = {}
    for child_id in order:
        child_meta = existing[child_id].meta
        page = child_meta.get("page")
        if child_meta.get("stroke_geometry_hash"):
            key = (page, child_meta["stroke_geometry_hash"])
            by_geometry.setdefault(key, []).append(child_id)
        for name in _child_names(child_meta):
            names = by_name.setdefault((page, name), [])
            if child_id not in names:
                names.append(child_id)

    claimed: set[int] = set()
    matches: list[int | None] = [None] * len(annotations)

    def match_pass(candidates_for, accept) -> None:
        for i, ann in enumerate(annotations):
            if matches[i] is not None:
                continue
            for child_id in candidates_for(ann):
                if child_id not in claimed and accept(child_id, ann):
                    claimed.add(child_id)
                    matches[i] = child_id
                    break

    def on_page(
        index: dict[tuple[Any, str], list[int]], keys: Iterable[str], page: int
    ) -> list[int]:
        # A child with no page recorded (hand-made or very old) matches on any page
        found: list[int] = []
        for key in keys:
            for p in (page, None):
                found += [c for c in index.get((p, key), []) if c not in found]
        return sorted(found, key=order.index)

    def box_overlaps(child_id: int, ann: ExtractedAnnotation) -> bool:
        return _overlaps(existing[child_id].meta.get("bbox"), ann.bbox)

    match_pass(
        lambda ann: on_page(by_geometry, [ann.stroke_geometry_hash], ann.page),
        lambda child_id, ann: True,
    )
    match_pass(
        lambda ann: on_page(by_name, _ann_names(ann), ann.page),
        box_overlaps,
    )
    match_pass(
        lambda ann: [] if _ann_names(ann) else order,
        lambda child_id, ann: (
            not _child_names(existing[child_id].meta)
            and existing[child_id].meta.get("page") in (ann.page, None)
            and existing[child_id].meta.get("bbox") is not None
            and box_overlaps(child_id, ann)
        ),
    )

    # The enricher's own edits are logged as such in item_history, removals too
    pre_max = max_history_id(conn)
    touched: list[int] = []

    # A child whose annotation is gone (erased, or merged into another) is kept
    # and marked, not deleted: deleting would also drop its tags, collection
    # links, history and OCR text. It moves to a key no annotation can take, and
    # comes back to life if a later run matches it again.
    removed_at = _now_iso()
    for child_id, child in existing.items():
        if child_id in claimed or child.meta.get(REMOVED_KEY):
            continue
        conn.execute(
            "UPDATE items SET source_id = ?, title = ?, metadata = ? WHERE id = ?",
            (
                f"{parent_key}:removed:{child_id}",
                f"{REMOVED_TITLE_PREFIX}{child.title or ''}",
                json.dumps({**child.meta, REMOVED_KEY: removed_at}),
                child_id,
            ),
        )
        touched.append(child_id)

    # Keys held by this parent's former children (moved elsewhere by hand) are
    # taken, so a new child can't collide with one on UNIQUE(source_type, source_id)
    used_keys: set[str] = {
        row[0]
        for row in conn.execute(
            "SELECT source_id FROM items WHERE source_type = ? AND source_id LIKE ? "
            "AND parent_id IS NOT ?",
            (ANNOTATION_SOURCE_TYPE, f"{parent_key}:%", parent_id),
        )
    }
    plan = [
        (ann, match, _unique_source_id(parent_key, ann, used_keys))
        for ann, match in zip(annotations, matches, strict=True)
    ]
    # Park a child on a placeholder first only when another row in this run
    # (moved or inserted) wants the source_id it holds now, so swapping keys
    # cannot trip UNIQUE(source_type, source_id). Targets are unique, and removed
    # children sit on their own keys.
    wanted = {source_id: match for _, match, source_id in plan}
    for _, match, source_id in plan:
        if match is None:
            continue
        current = existing[match].source_id
        if current != source_id and current in wanted:
            conn.execute(
                "UPDATE items SET source_id = ? WHERE id = ?",
                (f"{parent_key}:pending:{match}", match),
            )

    for ann, match, source_id in plan:
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
            "annotation_ids": list(ann.member_ids),
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

        old_meta = existing[match].meta
        # OCR reads the strokes' area whatever the kind, so keep it for the same
        # strokes while the kind is still one OCR handles. A reclassified
        # annotation (say margin note to underline) gets its new content.
        if (
            old_meta.get("stroke_geometry_hash") == ann.stroke_geometry_hash
            and ann.kind in OCR_TARGET_KINDS
        ):
            for ocr_key in OCR_META_KEYS:
                if ocr_key in old_meta:
                    meta[ocr_key] = old_meta[ocr_key]
            if "ocr_version" in old_meta:
                content = None  # COALESCE below keeps the OCR'd content
        cur = conn.execute(
            """
            UPDATE items SET
                source_id = ?,
                title = ?,
                content = COALESCE(?, content),
                created_at = COALESCE(?, created_at),
                metadata = ?
            WHERE id = ?
              AND (source_id IS NOT ? OR title IS NOT ? OR content IS NOT COALESCE(?, content)
                   OR created_at IS NOT COALESCE(?, created_at) OR metadata IS NOT ?)
            """,
            (
                source_id,
                title,
                content,
                ann.created_at,
                json.dumps(meta),
                match,
                source_id,
                title,
                content,
                ann.created_at,
                json.dumps(meta),
            ),
        )
        if cur.rowcount:
            touched.append(match)

    mark_recent_history(conn, pre_max, "enricher", item_ids=touched)


def _overlaps(old_bbox: Any, bbox: tuple[float, float, float, float]) -> bool:
    """Whether two (x0, y0, x1, y1) boxes intersect. A missing old box counts."""
    try:
        x0, y0, x1, y1 = (float(v) for v in old_bbox)
    except (TypeError, ValueError):
        return True
    return x0 <= bbox[2] and bbox[0] <= x1 and y0 <= bbox[3] and bbox[1] <= y1


def _parse_meta(raw: Any) -> dict[str, Any]:
    """Item metadata JSON as a dict; anything unreadable or not an object is {}."""
    try:
        meta = json.loads(raw) if raw else {}
    except (TypeError, json.JSONDecodeError):
        return {}
    return meta if isinstance(meta, dict) else {}


class _Child(NamedTuple):
    source_id: str | None
    title: str | None
    meta: dict[str, Any]


def _load_existing_children(conn: sqlite3.Connection, parent_id: int) -> dict[int, _Child]:
    """Each annotation child of the parent, by item id."""
    rows = conn.execute(
        "SELECT id, source_id, title, metadata FROM items "
        "WHERE parent_id = ? AND source_type = ? ORDER BY id",
        (parent_id, ANNOTATION_SOURCE_TYPE),
    ).fetchall()
    return {row[0]: _Child(row[1], row[2], _parse_meta(row[3])) for row in rows}


def _child_names(meta: dict[str, Any]) -> list[str]:
    """A child's stroke /NM names; older children stored only the first."""
    names = meta.get("annotation_ids") or []
    first = meta.get("annotation_id")
    return [n for n in ([first] if first else []) + list(names) if isinstance(n, str) and n]


def _ann_names(ann: ExtractedAnnotation) -> list[str]:
    return list(
        dict.fromkeys([*([ann.annotation_id] if ann.annotation_id else []), *ann.member_ids])
    )


def _unique_source_id(parent_key: str, ann: ExtractedAnnotation, used: set[str]) -> str:
    """Parent-scoped source_id for an annotation child: `<parent>:p<page>:<key>`.

    The key is the /NM, else the geometry hash. The page is part of it because
    /NM values are only unique within one page (PyMuPDF restarts `fitz-A0`,
    `fitz-A1`, ... on each page, in every file). A `#n` suffix is left only for
    a repeat within one page, such as identical strokes drawn twice.
    """
    base = f"{parent_key}:p{ann.page}:{ann.annotation_id or ann.stroke_geometry_hash}"
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
    - A row exists for another file: update it in place, and keep the previous
      file as a `source_pdf_version` row (its `created_at` is when it was
      replaced). The history lives in media rows, not item metadata, because a
      re-sync's upsert replaces the metadata. Old files stay on disk; media files
      are content-addressed, so each version keeps its own path.
    - Extra rows left by older code are kept as versions and removed.

    A missing `url` keeps the existing row's url (e.g. a Drive link) for the same
    file; a different file without a url gets none.
    `source_origin` is set when the file changes; the same file keeps its origin.
    Does not commit. Returns the media row ID.
    """
    rows = conn.execute(
        "SELECT id, local_path, url, source_origin, sha256 FROM item_media "
        "WHERE item_id = ? AND media_type = ? ORDER BY id",
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
    seen: set[str] = set()
    for row in rows:
        # Only a real earlier file is a version, once each. A url-only row, or one
        # whose file is gone (so the same Drive file was downloaded again), is not.
        old_path = row[1]
        if old_path and old_path != local_path and old_path not in seen:
            seen.add(old_path)
            # Its file in the store, else the media folder (#217)
            if media_file({"local_path": old_path, "sha256": row[4]}, config) is not None:
                _record_previous_source_pdf(conn, item_id, row)
    changed = local_path not in {row[1] for row in rows}
    # A different file without a url of its own must not keep the old file's link.
    # The same file keeps its url, or takes one from a legacy row being folded in.
    old_url = next((row[2] for row in rows if row[2]), None)
    new_url = url if changed else (url or old_url)
    if len(rows) > 1:
        extra_ids = [row[0] for row in rows[1:]]
        conn.execute(
            f"DELETE FROM item_media WHERE id IN ({','.join('?' * len(extra_ids))})",
            extra_ids,
        )
    conn.execute(
        """
        UPDATE item_media SET local_path = ?, url = ?, mime_type = ?,
            alt_text = 'original PDF', position = 0,
            source_origin = CASE WHEN ? THEN ? ELSE source_origin END,
            -- another file is not the stored one; the next adopt stores it
            sha256 = CASE WHEN local_path IS ? THEN sha256 ELSE NULL END
        WHERE id = ?
        """,
        (local_path, new_url, PDF_MIME, changed, source_origin, local_path, keep_id),
    )
    return keep_id


def _record_previous_source_pdf(
    conn: sqlite3.Connection, item_id: int, row: sqlite3.Row | tuple
) -> None:
    """Keep a replaced source PDF as a `source_pdf_version` row.

    Always a new row, even for a path seen before, so A→B→A→B keeps every step.
    """
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM item_media WHERE item_id = ? AND media_type = ?",
        (item_id, SOURCE_PDF_VERSION_MEDIA_TYPE),
    ).fetchone()
    conn.execute(
        """INSERT INTO item_media
           (item_id, media_type, url, local_path, mime_type, alt_text, position, source_origin,
            sha256)
           VALUES (?, ?, ?, ?, ?, 'previous PDF', ?, ?, ?)""",
        (item_id, SOURCE_PDF_VERSION_MEDIA_TYPE, row[2], row[1], PDF_MIME, count, row[3], row[4]),
    )


def _load_item_metadata(conn: sqlite3.Connection, item_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT metadata FROM items WHERE id = ?", (item_id,)).fetchone()
    return _parse_meta(row[0]) if row else {}


def _update_parent_item(
    conn: sqlite3.Connection,
    item_id: int,
    content: str,
    enriched: EnrichedPdf,
    *,
    status: str,
) -> None:
    meta = _load_item_metadata(conn, item_id)
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
