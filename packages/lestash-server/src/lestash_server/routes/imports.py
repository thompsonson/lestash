"""File import endpoint."""

import hashlib
import json
import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, UploadFile
from lestash.core.database import resolve_parent_id
from lestash.importers import merge_with_stored, parse_json_items, parse_zip

from lestash_server.deps import get_config, get_db
from lestash_server.models import (
    DriveImportRequest,
    DriveSyncRequest,
    DriveSyncResponse,
    ImportResponse,
)

__all__ = ["parse_zip", "router"]  # parse_zip lives in lestash.importers; kept for callers

logger = logging.getLogger(__name__)

router = APIRouter(tags=["import"])

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB


@router.post("/api/import", response_model=ImportResponse)
async def import_file(
    file: UploadFile,
    page_type: str = Form("auto"),
    source_url: str | None = Form(None),
    notes: str | None = Form(None),
):
    """Import items from an uploaded file.

    Supports:
    - JSON array of items (.json)
    - ZIP files with known structures (Google Takeout, Claude export)
    - HTML pages with auto-detection (Gemini, etc.)
    - PDF documents (text extracted via Docling, original saved as media)

    Optional form fields for HTML imports:
    - page_type: "auto", "gemini", "chatgpt", "article", or "unknown"
    - source_url: original URL of the page
    - notes: user annotations
    """
    data = await file.read()
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 50MB)")

    filename = file.filename or ""
    items = []
    source_type = "import"
    errors: list[str] = []

    try:
        if filename.endswith(".json"):
            items = parse_json_items(data)
            source_type = "json"
        elif filename.endswith(".zip"):
            items, source_type = parse_zip(data)
        elif filename.endswith((".html", ".htm")):
            from lestash_server.parsers.html_page import parse_html_page

            html_text = data.decode("utf-8", errors="replace")
            items, source_type = parse_html_page(
                html_text,
                page_type=page_type,
                source_url=source_url,
                notes=notes,
            )
        elif filename.lower().endswith(".pdf"):
            return _import_pdf(data, filename, errors)
        else:
            # Try JSON first, then fail
            try:
                items = parse_json_items(data)
                source_type = "json"
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unsupported file format: {filename}. Use .json, .zip, .html, or .pdf"
                    ),
                ) from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None

    with get_db() as conn:
        items_added, import_errors = _insert_items_with_parents(conn, items)
        errors.extend(import_errors)

    return ImportResponse(
        status="completed",
        source_type=source_type,
        items_added=items_added,
        items_updated=0,
        errors=errors,
    )


def _upsert_item(conn, item, parent_id=None):
    """Insert or update a single item. Returns the row ID, or None if skipped.

    An existing parent is kept when none is given. The ``_parent_source_id`` marker is
    dropped once the parent is known, and kept otherwise so a later sync can link it.
    """
    merge_with_stored(conn, item)
    metadata = dict(item.metadata) if item.metadata else {}
    if parent_id or item.parent_id:
        metadata.pop("_parent_source_id", None)
    metadata_json = json.dumps(metadata) if metadata else None
    source_id = item.source_id or item.url

    if not source_id:
        logger.warning(
            "Skipping item with no source_id or url: %s",
            (item.title or item.content[:50]) if item.content else "empty",
        )
        return None

    cursor = conn.execute(
        """
        INSERT INTO items (
            source_type, source_id, url, title, content,
            author, created_at, is_own_content, metadata, parent_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_type, source_id) DO UPDATE SET
            url = excluded.url,
            content = excluded.content,
            title = excluded.title,
            author = excluded.author,
            is_own_content = excluded.is_own_content,
            metadata = excluded.metadata,
            parent_id = COALESCE(excluded.parent_id, items.parent_id)
        RETURNING id
        """,
        (
            item.source_type,
            source_id,
            item.url,
            item.title,
            item.content,
            item.author,
            item.created_at,
            item.is_own_content,
            metadata_json,
            parent_id or item.parent_id,
        ),
    )
    # RETURNING gives the row's id on insert and on update. lastrowid doesn't: after
    # ON CONFLICT DO UPDATE it still holds the previous insert's id.
    return cursor.fetchone()[0]


def _insert_items_with_parents(conn, items):
    """Two-pass insert: parents first, then children with resolved parent_id."""
    parents = []
    children = []
    for item in items:
        if item.metadata and item.metadata.get("_parent_source_id"):
            children.append(item)
        else:
            parents.append(item)

    items_added = 0
    errors: list[str] = []
    # Map source_id → DB id for parent resolution
    parent_id_map: dict[str, int] = {}

    # Pass 1: insert parents
    for item in parents:
        try:
            row_id = _upsert_item(conn, item)
            if row_id:
                # Use the effective source_id (same logic as _upsert_item)
                effective_id = item.source_id or item.url or ""
                parent_id_map[effective_id] = row_id
                items_added += 1
        except Exception as e:
            errors.append(f"Failed to import parent: {e}")

    # Pass 2: insert children with resolved parent_id
    for item in children:
        try:
            parent_source_id = item.metadata["_parent_source_id"]
            resolved_parent_id = parent_id_map.get(parent_source_id)
            if resolved_parent_id is None:
                # Parent from an earlier import or sync (e.g. a liked YouTube video)
                resolve_parent_id(conn, item)
                resolved_parent_id = item.parent_id
            row_id = _upsert_item(conn, item, parent_id=resolved_parent_id)
            if row_id:
                items_added += 1
        except Exception as e:
            errors.append(f"Failed to import child: {e}")

    # Also handle items without parent markers (flat items)
    conn.commit()
    return items_added, errors


def _import_pdf(data: bytes, filename: str, errors: list[str]) -> ImportResponse:
    """Import a PDF: extract markdown via Docling, save original as media."""
    from lestash.core.database import save_media_file
    from lestash.core.pdf_enrich.persistence import set_source_pdf
    from lestash.core.text_extract import extract_content
    from lestash.models.item import ItemCreate

    sha = hashlib.sha256(data).hexdigest()[:16]
    display_name = filename.rsplit("/", 1)[-1]
    title = display_name[:-4] if display_name.lower().endswith(".pdf") else display_name

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tf.write(data)
        tmp_path = Path(tf.name)
    try:
        markdown = extract_content(tmp_path, "application/pdf")
    finally:
        tmp_path.unlink(missing_ok=True)

    item = ItemCreate(
        source_type="pdf",
        source_id=sha,
        title=title,
        content=markdown or f"[PDF: {display_name}]",
        is_own_content=True,
        metadata={"filename": display_name, "size_bytes": len(data)},
    )

    items_added = 0
    enrich_item_id: int | None = None
    with get_db() as conn:
        item_id = _upsert_item(conn, item)
        if item_id:
            rel_path = save_media_file(item_id, data, display_name, get_config())
            set_source_pdf(conn, get_config(), item_id, local_path=rel_path, source_origin="upload")
            items_added = 1
            enrich_item_id = item_id
        conn.commit()

    if enrich_item_id is not None:
        from lestash.core.pdf_enrich import enrich_item

        try:
            enrich_item(enrich_item_id)
        except Exception as e:
            logger.exception("Failed to enrich uploaded PDF item %d", enrich_item_id)
            errors.append(f"enrich: {e}")

    return ImportResponse(
        status="completed",
        source_type="pdf",
        items_added=items_added,
        items_updated=0,
        errors=errors,
    )


@router.post("/api/import/drive", response_model=list[ImportResponse])
def import_from_drive(body: DriveImportRequest):
    """Download files from Google Drive and import them.

    Requires Google auth to be configured via 'lestash google auth'. A plain ``def``, so
    FastAPI runs it in a worker thread: a multi-GB download and parse must not stall the
    event loop.
    """
    from lestash.core.google_auth import download_drive_file, extract_drive_file_id

    results = []
    for file_id_or_url in body.file_ids:
        file_id = extract_drive_file_id(file_id_or_url)
        try:
            path = download_drive_file(file_id)

            if path.name.endswith(".zip"):
                # Read from disk: Takeout parts can be several GB
                items, source_type = parse_zip(path)
            elif path.name.endswith(".json"):
                items = parse_json_items(path.read_bytes())
                source_type = "json"
            else:
                results.append(
                    ImportResponse(
                        status="error",
                        source_type="unknown",
                        items_added=0,
                        items_updated=0,
                        errors=[f"Unsupported file: {path.name}"],
                    )
                )
                continue

            with get_db() as conn:
                items_added, import_errors = _insert_items_with_parents(conn, items)

            results.append(
                ImportResponse(
                    status="completed",
                    source_type=source_type,
                    items_added=items_added,
                    items_updated=0,
                    errors=import_errors,
                )
            )
        except ValueError as e:
            results.append(
                ImportResponse(
                    status="error",
                    source_type="unknown",
                    items_added=0,
                    items_updated=0,
                    errors=[str(e)],
                )
            )
        except Exception as e:
            results.append(
                ImportResponse(
                    status="error",
                    source_type="unknown",
                    items_added=0,
                    items_updated=0,
                    errors=[f"Drive download failed for {file_id}: {e}"],
                )
            )

    return results


@router.post("/api/import/drive/sync", response_model=DriveSyncResponse)
def sync_from_drive(body: DriveSyncRequest):
    """Import Google Drive files/folders with Docling markdown conversion.

    Accepts Drive URLs, Google Docs URLs, folder URLs, or bare file IDs.
    Files are downloaded, converted to markdown, and stored as items.
    """
    from lestash.core.database import upsert_item
    from lestash.core.google_drive import (
        classify_drive_url,
        sync_drive_folder,
        sync_single_file,
    )
    from lestash.core.pdf_enrich import attach_source_pdf_and_enrich

    items_added = 0
    items_skipped = 0
    errors: list[str] = []
    pdf_followups: list[tuple[int, bytes, str, str | None]] = []

    for url in body.urls:
        try:
            url_type, file_id = classify_drive_url(url)

            if url_type == "folder":
                with get_db() as conn:
                    for item, pdf_bytes, filename in sync_drive_folder(file_id, recursive=True):
                        try:
                            item_id = upsert_item(conn, item)
                            items_added += 1
                            if pdf_bytes:
                                drive_url = (item.metadata or {}).get("drive_web_link")
                                pdf_followups.append((item_id, pdf_bytes, filename, drive_url))
                        except Exception as e:
                            errors.append(f"{item.title}: {e}")
                    conn.commit()
            elif url_type == "file":
                item, pdf_bytes, filename = sync_single_file(file_id)
                with get_db() as conn:
                    item_id = upsert_item(conn, item)
                    conn.commit()
                items_added += 1
                if pdf_bytes:
                    drive_url = (item.metadata or {}).get("drive_web_link")
                    pdf_followups.append((item_id, pdf_bytes, filename, drive_url))
            else:
                errors.append(f"Could not parse URL: {url}")
                items_skipped += 1
        except Exception as e:
            logger.exception("Failed to sync %s", url)
            errors.append(f"{url}: {e}")

    for item_id, pdf_bytes, filename, drive_url in pdf_followups:
        try:
            attach_source_pdf_and_enrich(item_id, pdf_bytes, filename, drive_url=drive_url)
        except Exception as e:
            logger.exception("Failed to enrich item %d", item_id)
            errors.append(f"enrich item {item_id}: {e}")

    return DriveSyncResponse(
        status="completed" if not errors else "completed_with_errors",
        items_added=items_added,
        items_skipped=items_skipped,
        errors=errors,
    )
