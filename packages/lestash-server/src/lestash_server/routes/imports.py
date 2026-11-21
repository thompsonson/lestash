"""File import endpoint."""

import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Form, HTTPException, UploadFile
from lestash.core.activities import record_activities, record_saved
from lestash.core.agents import link_agents
from lestash.core.database import resolve_parent_id
from lestash.core.links import link_items
from lestash.core.sync import dedupe_notebooks
from lestash.core.text_files import SOURCE_TYPE as TEXT_SOURCE
from lestash.core.text_files import add_text_file, text_format
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
    parent_id: int | None = Form(None),
):
    """Import items from an uploaded file.

    Supports:
    - JSON array of items (.json)
    - ZIP files with known structures (Google Takeout, Claude export)
    - HTML pages with auto-detection (Gemini, etc.)
    - PDF documents (text extracted via Docling, original saved as media)
    - Text and Markdown files (.txt, .md), kept as their text

    With ``parent_id``, a single page or file goes under that item (+ Add → File); one
    already held stays where it is. ``item_id`` in the reply names it.

    Optional form fields for HTML imports:
    - page_type: "auto", "gemini", "chatgpt", "article", or "unknown"
    - source_url: original URL of the page
    - notes: user annotations
    """
    data = await file.read()
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 50MB)")

    filename = file.filename or ""
    name = filename.lower()  # suffixes match in any case: PAGE.HTML is a page
    if parent_id is not None:
        with get_db() as conn:
            if not conn.execute("SELECT 1 FROM items WHERE id = ?", (parent_id,)).fetchone():
                raise HTTPException(status_code=404, detail=f"Item {parent_id} not found")
        if not name.endswith((".html", ".htm", ".pdf")) and not text_format(filename):
            raise HTTPException(
                status_code=400, detail="Only a single page or file can go under an item"
            )
    items = []
    source_type = "import"
    errors: list[str] = []

    try:
        if name.endswith(".json"):
            items = parse_json_items(data)
            source_type = "json"
        elif name.endswith(".zip"):
            items, source_type = parse_zip(data)
        elif name.endswith((".html", ".htm")):
            from lestash_server.parsers.html_page import parse_html_page

            html_text = data.decode("utf-8", errors="replace")
            items, source_type = parse_html_page(
                html_text,
                page_type=page_type,
                source_url=source_url,
                notes=notes,
            )
        elif name.endswith(".pdf"):
            return _import_pdf(data, filename, errors, parent_id)
        elif text_format(filename):
            return _import_text(data, filename, parent_id)
        else:
            # Try JSON first, then fail
            try:
                items = parse_json_items(data)
                source_type = "json"
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unsupported file format: {filename}. "
                        "Use .json, .zip, .html, .pdf, .txt or .md"
                    ),
                ) from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None

    # A single page you shared or uploaded is something you saved (#357); a bulk
    # .zip or .json import is not, so it doesn't fill the Timeline
    one_page = name.endswith((".html", ".htm"))
    if one_page:
        # A page that gives no date is dated when saved, so it sorts among an item's
        # parts and on its day, as a share does
        now = datetime.now(UTC)
        items = [i if i.created_at else i.model_copy(update={"created_at": now}) for i in items]
    with get_db() as conn:
        # A bulk .zip or .json import counts every item it stores, as before
        placed: list[tuple[int, bool]] | None = [] if one_page else None
        items_added, import_errors = _insert_items_with_parents(
            conn, items, saved=one_page, parent_id=parent_id if one_page else None, placed=placed
        )
        errors.extend(import_errors)

    return ImportResponse(
        status="completed",
        source_type=source_type,
        items_added=items_added,
        items_updated=0,
        errors=errors,
        item_id=placed[0][0] if placed else None,
        items_held=sum(1 for _, held in placed if held) if placed else 0,
    )


def _place(conn, item_id: int, parent_id: int | None, new: bool) -> None:
    """Put a newly added item under ``parent_id``; one already held is not moved."""
    if parent_id is not None and new and item_id != parent_id:
        conn.execute("UPDATE items SET parent_id = ? WHERE id = ?", (parent_id, item_id))


def _import_text(data: bytes, filename: str, parent_id: int | None) -> ImportResponse:
    """Import a .txt or .md file as one item holding its text."""
    with get_db() as conn:
        added = add_text_file(conn, data, filename)
        _place(conn, added.item_id, parent_id, added.new)
        record_saved(conn, added.item_id)  # saved now, also when held already (#357)
        conn.commit()
    return ImportResponse(
        status="completed",
        source_type=TEXT_SOURCE,
        items_added=1 if added.new else 0,
        items_updated=0,
        errors=[],
        item_id=added.item_id,
        items_held=0 if added.new else 1,
    )


def _upsert_item(conn, item, parent_id=None):
    """Insert or update a single item. Returns the row ID, or None if skipped.

    An existing parent is kept when none is given. The ``_parent_source_id`` marker is
    dropped once the parent is known, and kept otherwise so a later sync can link it.
    """
    if not merge_with_stored(conn, item):
        return None  # a stored item already holds it
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
    item_id = cursor.fetchone()[0]
    # An upload's activities are yours: an actor_id from outside names no known person
    record_activities(
        conn, item_id, [a.model_copy(update={"actor_id": None}) for a in item.activities or []]
    )
    return item_id


def _insert_items_with_parents(
    conn,
    items,
    saved: bool = False,
    parent_id: int | None = None,
    placed: list[tuple[int, bool]] | None = None,
):
    """Two-pass insert: parents first, then children with resolved parent_id.

    With ``saved``, each top-level item gets a 'saved' activity now (#357). With
    ``parent_id``, each new top-level item goes under that item; one already held is
    not moved. ``placed``, when given, gets each top-level item stored, as
    (id, held already), and the count then leaves out items held already, turns
    included: they are refreshed, not added. Returns the count added and the errors.
    """
    parents = []
    children = []
    for item in items:
        if item.metadata and item.metadata.get("_parent_source_id"):
            children.append(item)
        else:
            parents.append(item)

    items_added = 0
    stored = 0  # added or refreshed: what the steps after the insert look at
    errors: list[str] = []
    # Map source_id → DB id for parent resolution
    parent_id_map: dict[str, int] = {}
    # Pass 1: insert parents
    for item in parents:
        try:
            held = (parent_id is not None or placed is not None) and _held(conn, item)
            under = None if held or parent_id is None else parent_id
            row_id = _upsert_item(conn, item, parent_id=under)
            if row_id and placed is not None:
                placed.append((row_id, held))
            if row_id and saved:
                record_saved(conn, row_id)
            if row_id:
                # Use the effective source_id (same logic as _upsert_item)
                effective_id = item.source_id or item.url or ""
                parent_id_map[effective_id] = row_id
                stored += 1
                if not (placed is not None and held):
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
            held = placed is not None and _held(conn, item)
            row_id = _upsert_item(conn, item, parent_id=resolved_parent_id)
            if row_id:
                stored += 1
            if row_id and not held:
                items_added += 1
        except Exception as e:
            errors.append(f"Failed to import child: {e}")

    # Also handle items without parent markers (flat items)
    conn.commit()
    notebooks = any(
        item.source_type == "notebooklm" and (item.metadata or {}).get("type") == "notebook"
        for item in items
    )
    if stored and notebooks:
        dedupe_notebooks(conn, "an import")
    if stored:
        # Links are derived: a failure leaves the items stored; the next sync catches up
        try:
            link_items(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("Linking items failed after an import")
        try:
            link_agents(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("Recording agents failed after an import")
    return items_added, errors


def _held(conn, item) -> bool:
    """Whether LeStash already holds this item, by its key."""
    source_id = item.source_id or item.url
    return bool(
        source_id
        and conn.execute(
            "SELECT 1 FROM items WHERE source_type = ? AND source_id = ?",
            (item.source_type, source_id),
        ).fetchone()
    )


def _import_arxiv_pdf(
    data: bytes, filename: str, errors: list[str], parent_id: int | None
) -> ImportResponse | None:
    """A shared arXiv paper's PDF: the paper's arXiv item, with this file as its source
    PDF (#398). None if the file isn't an arXiv paper, or arXiv can't be reached, so
    it is added as a plain PDF."""
    try:
        from lestash_arxiv.share import arxiv_id_from_pdf, attach_pdf, import_paper
    except ImportError:
        return None
    arxiv_id = arxiv_id_from_pdf(data, filename)
    if arxiv_id is None:
        return None
    try:
        with get_db() as conn:
            imported = import_paper(conn, arxiv_id, parent_id=parent_id)
    except Exception:
        logger.exception("Could not fetch arXiv paper %s; adding the PDF as it is", arxiv_id)
        return None
    if imported is None:
        return None
    if not imported.has_pdf:
        try:
            status = attach_pdf(imported.item_id, config=get_config(), data=data)
            if status == "failed":
                errors.append("enrich: the PDF could not be read")
        except Exception as e:
            logger.exception("Failed to store the PDF of arXiv item %d", imported.item_id)
            errors.append(f"enrich: {e}")
    return ImportResponse(
        status="completed",
        source_type="arxiv",
        items_added=1 if imported.created else 0,
        items_updated=0,
        errors=errors,
        item_id=imported.item_id,
        items_held=0 if imported.created else 1,
    )


def _import_pdf(
    data: bytes, filename: str, errors: list[str], parent_id: int | None = None
) -> ImportResponse:
    """Import a PDF: extract markdown via Docling, save original as media.

    A file already held is not added again (#338). An arXiv paper becomes its arXiv
    item instead (#398).
    """
    from lestash.core.pdfs import add_pdf

    paper = _import_arxiv_pdf(data, filename, errors, parent_id)
    if paper is not None:
        return paper

    with get_db() as conn:
        added = add_pdf(conn, get_config(), data, filename)
        _place(conn, added.item_id, parent_id, added.new)
        # Saved now, also when the file was held already (#357)
        record_saved(conn, added.item_id)
        conn.commit()
    items_added = 1 if added.new else 0
    enrich_item_id = added.item_id if added.new else None

    if enrich_item_id is not None:
        from lestash.core.pdf_enrich import enrich_item

        try:
            result = enrich_item(enrich_item_id, config=get_config())
            if result.status == "failed":
                errors.append(f"enrich: {result.message}")
        except Exception as e:
            logger.exception("Failed to enrich uploaded PDF item %d", enrich_item_id)
            errors.append(f"enrich: {e}")

    return ImportResponse(
        status="completed",
        source_type="pdf",
        items_added=items_added,
        items_updated=0,
        errors=errors,
        item_id=added.item_id,
        items_held=0 if added.new else 1,
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
        store_folder_file,
        sync_drive_folder,
        sync_single_file,
    )
    from lestash.core.pdf_enrich import attach_source_pdf_and_enrich

    items_added = 0
    items_skipped = 0
    items_held = 0
    errors: list[str] = []
    pdf_followups: list[tuple[int, bytes, str, str | None]] = []

    for url in body.urls:
        try:
            url_type, file_id = classify_drive_url(url)

            if url_type == "folder":
                with get_db() as conn:
                    for item, pdf_bytes, filename in sync_drive_folder(file_id, recursive=True):
                        try:
                            item_id = store_folder_file(conn, item)
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
                    if body.parent_id is not None:
                        # A file already held stays where it is; a new one goes under
                        # the item it was added to (+ Add → New link)
                        held = conn.execute(
                            "SELECT id FROM items WHERE source_type = ? AND source_id = ?",
                            (item.source_type, item.source_id),
                        ).fetchone()
                        if held is None:
                            item.parent_id = body.parent_id
                        else:
                            items_held += 1
                    item_id = upsert_item(conn, item)
                    # Importing a file is saving it now, under an item or alone, so it
                    # shows on today's Timeline, again if it was imported before (#387)
                    record_saved(conn, item_id)
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
            result = attach_source_pdf_and_enrich(
                item_id, pdf_bytes, filename, drive_url=drive_url, config=get_config()
            )
            if result.status == "failed":
                errors.append(f"enrich item {item_id}: {result.message}")
        except Exception as e:
            logger.exception("Failed to enrich item %d", item_id)
            errors.append(f"enrich item {item_id}: {e}")

    return DriveSyncResponse(
        status="completed" if not errors else "completed_with_errors",
        items_added=items_added,
        items_skipped=items_skipped,
        errors=errors,
        items_held=items_held,
    )
