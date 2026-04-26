"""End-to-end runner: from item ID to persisted enrichment.

Public entry points:

    enrich_item(item_id) -> EnrichmentResult
    backfill_source_pdfs() -> BackfillStats

Both are idempotent. `enrich_item` skips when the stored
(pdf_sha256, extractor_version) matches the current run unless `force=True`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lestash.core.config import Config
from lestash.core.database import (
    add_item_media,
    get_connection,
    get_media_dir,
    save_media_file,
)

from .extractor import enrich_pdf
from .persistence import (
    is_already_enriched,
    mark_source_unavailable,
    persist_enrichment,
)
from .version import EXTRACTOR_VERSION

logger = logging.getLogger(__name__)

SOURCE_PDF_MEDIA_TYPE = "source_pdf"
PDF_MIME = "application/pdf"

EnrichmentStatus = Literal["enriched", "skipped", "source_unavailable", "failed"]


@dataclass
class EnrichmentResult:
    item_id: int
    status: EnrichmentStatus
    images: int = 0
    annotations: int = 0
    message: str | None = None


@dataclass
class BackfillStats:
    inspected: int = 0
    backfilled: int = 0
    already_present: int = 0
    unavailable: int = 0


def enrich_item(
    item_id: int, *, config: Config | None = None, force: bool = False
) -> EnrichmentResult:
    """Enrich a single item by ID. Resolves the source PDF, runs the
    extractor, persists the result. Idempotent unless `force=True`."""
    config = config or Config.load()
    with get_connection(config) as conn:
        item_row = conn.execute(
            "SELECT id, source_type, source_id, metadata FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if not item_row:
            return EnrichmentResult(item_id=item_id, status="failed", message="item not found")

        try:
            pdf_path = _resolve_source_pdf(conn, config, item_row)
        except _SourceUnavailable as exc:
            mark_source_unavailable(conn, item_id)
            return EnrichmentResult(item_id=item_id, status="source_unavailable", message=str(exc))

        try:
            enriched = enrich_pdf(pdf_path)
        except Exception as exc:
            logger.exception("Extractor crashed for item %d", item_id)
            return EnrichmentResult(item_id=item_id, status="failed", message=str(exc))

        if not force and is_already_enriched(conn, item_id, EXTRACTOR_VERSION, enriched.pdf_sha256):
            return EnrichmentResult(item_id=item_id, status="skipped")

        try:
            persist_enrichment(conn, config, item_id, enriched)
        except Exception as exc:
            logger.exception("Persistence failed for item %d", item_id)
            return EnrichmentResult(item_id=item_id, status="failed", message=str(exc))

        return EnrichmentResult(
            item_id=item_id,
            status="enriched",
            images=len(enriched.images),
            annotations=len(enriched.annotations),
        )


def attach_source_pdf_and_enrich(
    item_id: int,
    pdf_bytes: bytes,
    filename: str,
    *,
    drive_url: str | None = None,
    config: Config | None = None,
) -> EnrichmentResult:
    """Inline import path: persist the source PDF as a media row and run the
    enricher in one shot.

    Called from `sync()` (Drive) and the Kobo backup ingestion paths after
    the item has been upserted.
    """
    config = config or Config.load()
    with get_connection(config) as conn:
        rel_path = save_media_file(item_id, pdf_bytes, filename, config=config)
        add_item_media(
            conn,
            item_id,
            media_type=SOURCE_PDF_MEDIA_TYPE,
            url=drive_url,
            local_path=rel_path,
            mime_type=PDF_MIME,
            alt_text="original PDF",
            position=0,
            source_origin="sync",
            _commit=True,
        )
    return enrich_item(item_id, config=config)


def list_pdf_items(conn: sqlite3.Connection) -> list[int]:
    """Return IDs of items whose source is a PDF (Google Drive or otherwise).

    Filters by metadata.mime_type or by the presence of a source_pdf media row.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT i.id FROM items i
        LEFT JOIN item_media m ON m.item_id = i.id AND m.media_type = ?
        WHERE m.id IS NOT NULL
           OR (i.metadata IS NOT NULL AND i.metadata LIKE '%application/pdf%')
        ORDER BY i.id
        """,
        (SOURCE_PDF_MEDIA_TYPE,),
    ).fetchall()
    return [row[0] for row in rows]


def backfill_source_pdfs(*, config: Config | None = None) -> BackfillStats:
    """One-shot backfill: for every Google-Drive-sourced PDF item that lacks a
    `source_pdf` media row, download the file from Drive and create the row.

    Idempotent: items that already have a source_pdf row are skipped.
    """
    config = config or Config.load()
    stats = BackfillStats()
    with get_connection(config) as conn:
        rows = conn.execute(
            """
            SELECT id, metadata FROM items
            WHERE source_type = 'google-drive'
              AND metadata IS NOT NULL
              AND metadata LIKE '%application/pdf%'
            ORDER BY id
            """
        ).fetchall()
        for row in rows:
            stats.inspected += 1
            item_id = row[0]
            existing = conn.execute(
                "SELECT 1 FROM item_media WHERE item_id = ? AND media_type = ?",
                (item_id, SOURCE_PDF_MEDIA_TYPE),
            ).fetchone()
            if existing:
                stats.already_present += 1
                continue
            try:
                meta = json.loads(row[1])
            except (TypeError, json.JSONDecodeError):
                stats.unavailable += 1
                continue
            try:
                _create_source_pdf_row_from_drive(conn, config, item_id, meta)
                stats.backfilled += 1
            except _SourceUnavailable:
                mark_source_unavailable(conn, item_id)
                stats.unavailable += 1
    return stats


# --- Internal -----------------------------------------------------------


class _SourceUnavailable(Exception):
    pass


def _resolve_source_pdf(conn: sqlite3.Connection, config: Config, item_row: sqlite3.Row) -> Path:
    """Return a filesystem path to the item's source PDF.

    Order of resolution:
        1. local_path on a source_pdf media row
        2. drive_id on either the source_pdf row's url or items.metadata
           — download into the cache and persist a source_pdf row.

    Raises `_SourceUnavailable` if neither path works.
    """
    item_id = item_row[0]
    media_dir = get_media_dir(config)

    media_row = conn.execute(
        """
        SELECT local_path, url FROM item_media
        WHERE item_id = ? AND media_type = ?
        ORDER BY id LIMIT 1
        """,
        (item_id, SOURCE_PDF_MEDIA_TYPE),
    ).fetchone()
    if media_row:
        local = media_row[0]
        if local:
            candidate = media_dir / local
            if candidate.exists():
                return candidate
        # Fall through to Drive re-download

    metadata = {}
    if item_row[3]:
        try:
            metadata = json.loads(item_row[3])
        except (TypeError, json.JSONDecodeError):
            metadata = {}
    drive_id = metadata.get("drive_id")
    if not drive_id:
        raise _SourceUnavailable("no source_pdf row and no drive_id in metadata")

    return _create_source_pdf_row_from_drive(conn, config, item_id, metadata)


def _create_source_pdf_row_from_drive(
    conn: sqlite3.Connection,
    config: Config,
    item_id: int,
    metadata: dict,
) -> Path:
    drive_id = metadata.get("drive_id")
    if not drive_id:
        raise _SourceUnavailable("metadata has no drive_id")

    title = metadata.get("title") or f"{drive_id}.pdf"
    name = title if title.lower().endswith(".pdf") else f"{title}.pdf"

    try:
        from lestash.core.google_auth import get_drive_service
        from lestash.core.google_drive import _download_file
    except Exception as exc:
        raise _SourceUnavailable(f"drive helpers unavailable: {exc}") from exc

    try:
        service = get_drive_service()
        cached_path = _download_file(service, drive_id, name)
    except Exception as exc:
        raise _SourceUnavailable(f"drive download failed: {exc}") from exc

    try:
        data = cached_path.read_bytes()
    except Exception as exc:
        raise _SourceUnavailable(f"could not read downloaded PDF: {exc}") from exc

    rel_path = save_media_file(item_id, data, name, config=config)
    add_item_media(
        conn,
        item_id,
        media_type=SOURCE_PDF_MEDIA_TYPE,
        url=metadata.get("drive_web_link"),
        local_path=rel_path,
        mime_type=PDF_MIME,
        alt_text="original PDF",
        position=0,
        source_origin="sync",
        _commit=False,
    )
    conn.commit()
    return get_media_dir(config) / rel_path
