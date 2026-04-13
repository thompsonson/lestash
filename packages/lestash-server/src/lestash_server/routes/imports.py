"""File import endpoint."""

import contextlib
import hashlib
import json
import logging
import tempfile
import zipfile
from datetime import UTC
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, UploadFile

from lestash_server.deps import get_config, get_db
from lestash_server.models import (
    DriveImportRequest,
    DriveSyncRequest,
    DriveSyncResponse,
    ImportResponse,
)
from lestash_server.parsers.json_items import parse_json_items

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
            items, source_type = _parse_zip(data)
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
    """Insert or update a single item. Returns the row ID, or None if skipped."""
    metadata = dict(item.metadata) if item.metadata else {}
    # Strip internal parent marker before storing
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
            parent_id = excluded.parent_id
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
    # Get the ID (works for both insert and upsert)
    if cursor.lastrowid:
        return cursor.lastrowid
    row = conn.execute(
        "SELECT id FROM items WHERE source_type = ? AND source_id = ?",
        (item.source_type, source_id),
    ).fetchone()
    return row[0] if row else None


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
    from lestash.core.database import add_item_media, save_media_file
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
    with get_db() as conn:
        item_id = _upsert_item(conn, item)
        if item_id:
            rel_path = save_media_file(item_id, data, display_name, get_config())
            add_item_media(
                conn,
                item_id,
                media_type="pdf",
                local_path=rel_path,
                mime_type="application/pdf",
                source_origin="upload",
            )
            items_added = 1
        conn.commit()

    return ImportResponse(
        status="completed",
        source_type="pdf",
        items_added=items_added,
        items_updated=0,
        errors=errors,
    )


@router.post("/api/import/drive", response_model=list[ImportResponse])
async def import_from_drive(body: DriveImportRequest):
    """Download files from Google Drive and import them.

    Requires Google auth to be configured via 'lestash google auth'.
    """
    from lestash.core.google_auth import download_drive_file, extract_drive_file_id

    results = []
    for file_id_or_url in body.file_ids:
        file_id = extract_drive_file_id(file_id_or_url)
        try:
            path = download_drive_file(file_id)
            data = path.read_bytes()

            if path.name.endswith(".zip"):
                items, source_type = _parse_zip(data)
            elif path.name.endswith(".json"):
                items = parse_json_items(data)
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
async def sync_from_drive(body: DriveSyncRequest):
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

    items_added = 0
    items_skipped = 0
    errors: list[str] = []

    for url in body.urls:
        try:
            url_type, file_id = classify_drive_url(url)

            if url_type == "folder":
                with get_db() as conn:
                    for item in sync_drive_folder(file_id, recursive=True):
                        try:
                            upsert_item(conn, item)
                            items_added += 1
                        except Exception as e:
                            errors.append(f"{item.title}: {e}")
                    conn.commit()
            elif url_type == "file":
                item = sync_single_file(file_id)
                with get_db() as conn:
                    upsert_item(conn, item)
                    conn.commit()
                items_added += 1
            else:
                errors.append(f"Could not parse URL: {url}")
                items_skipped += 1
        except Exception as e:
            logger.exception("Failed to sync %s", url)
            errors.append(f"{url}: {e}")

    return DriveSyncResponse(
        status="completed" if not errors else "completed_with_errors",
        items_added=items_added,
        items_skipped=items_skipped,
        errors=errors,
    )


def _parse_zip(data: bytes):
    """Parse a ZIP file, detecting the format from contents."""
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            names = zf.namelist()

            # Detect Google Keep Takeout
            keep_files = [n for n in names if n.startswith("Keep/") and n.endswith(".json")]
            if not keep_files:
                keep_files = [n for n in names if "/Keep/" in n and n.endswith(".json")]
            if keep_files:
                return _parse_google_keep_zip(zf, keep_files), "google-keep"

            # Detect Google Takeout with Gemini and/or NotebookLM
            gemini_convos = [
                n
                for n in names
                if ("Gemini" in n)
                and (n.endswith(".txt") or n.endswith(".json"))
                and "Conversation" in n
                and not n.endswith("/")
            ]
            nlm_files = [n for n in names if "NotebookLM" in n and not n.endswith("/")]

            if gemini_convos or nlm_files:
                all_items = []
                if gemini_convos:
                    all_items.extend(_parse_gemini_zip(zf, gemini_convos))
                if nlm_files:
                    all_items.extend(_parse_notebooklm_zip(zf, nlm_files, names))
                source_type = "takeout"
                if gemini_convos and not nlm_files:
                    source_type = "gemini"
                elif nlm_files and not gemini_convos:
                    source_type = "notebooklm"
                return all_items, source_type

            # Detect Mistral Le Chat export
            from lestash_server.parsers.mistral import detect_mistral_zip, parse_mistral_zip

            if detect_mistral_zip(names):
                return parse_mistral_zip(zf), "mistral"

            # Try generic JSON files in zip
            json_files = [n for n in names if n.endswith(".json")]
            if json_files:
                all_items = []
                for jf in json_files:
                    try:
                        all_items.extend(parse_json_items(zf.read(jf)))
                    except ValueError:
                        continue
                if all_items:
                    return all_items, "json"

            raise ValueError("ZIP file does not contain recognizable import data")
    except zipfile.BadZipFile:
        raise ValueError("Invalid ZIP file") from None


def _parse_google_keep_zip(zf, keep_files):
    """Parse Google Keep notes from a Takeout ZIP."""
    from lestash.models.item import ItemCreate

    items = []
    for name in keep_files:
        try:
            note = json.loads(zf.read(name))
            if note.get("isTrashed", False):
                continue

            content = note.get("textContent", "")
            if not content and note.get("listContent"):
                lines = []
                for li in note["listContent"]:
                    check = "[x]" if li.get("isChecked") else "[ ]"
                    lines.append(f"{check} {li.get('text', '')}")
                content = "\n".join(lines)

            if not content:
                continue

            # Convert microsecond timestamps
            created_us = note.get("createdTimestampUsec", 0)
            created_at = None
            if created_us:
                from datetime import datetime

                created_at = datetime.fromtimestamp(created_us / 1_000_000, tz=UTC)

            metadata: dict[str, object] = {}
            if note.get("labels"):
                metadata["labels"] = [lbl["name"] for lbl in note["labels"]]
            if note.get("color") and note["color"] != "DEFAULT":
                metadata["color"] = note["color"]
            if note.get("isPinned"):
                metadata["is_pinned"] = True
            if note.get("listContent"):
                metadata["list_items"] = [
                    {"text": li.get("text", ""), "is_checked": li.get("isChecked", False)}
                    for li in note["listContent"]
                ]

            items.append(
                ItemCreate(
                    source_type="google-keep",
                    source_id=name,
                    title=note.get("title"),
                    content=content,
                    created_at=created_at,
                    is_own_content=True,
                    metadata=metadata if metadata else None,
                )
            )
        except Exception:
            continue

    return items


def _parse_gemini_zip(zf, gemini_files):
    """Parse Gemini conversations from a Google Takeout ZIP.

    Gemini Takeout exports conversations as .txt files containing JSON with
    'conversation_turns' array of user_turn/system_turn pairs.
    """
    from datetime import datetime

    from lestash.models.item import ItemCreate

    items = []
    for name in gemini_files:
        try:
            raw = zf.read(name).decode("utf-8")
            data = json.loads(raw)
            turns = data.get("conversation_turns", [])
            if not turns:
                continue

            parts = []
            first_prompt = ""
            earliest_ts = None

            for turn in turns:
                if "user_turn" in turn:
                    ut = turn["user_turn"]
                    prompt = ut.get("prompt", "")
                    if prompt:
                        parts.append(f"**User:** {prompt}")
                        if not first_prompt:
                            first_prompt = prompt
                    ts = ut.get("turn_last_modified")
                    if ts and not earliest_ts:
                        earliest_ts = ts

                if "system_turn" in turn:
                    st = turn["system_turn"]
                    text_parts = st.get("text", [])
                    text = "\n".join(t.get("data", "") for t in text_parts if isinstance(t, dict))
                    if text:
                        parts.append(f"**Gemini:** {text}")

            if not parts:
                continue

            created_at = None
            if earliest_ts:
                with contextlib.suppress(ValueError, OSError):
                    created_at = datetime.fromisoformat(str(earliest_ts).replace("Z", "+00:00"))

            title = first_prompt[:80] + ("..." if len(first_prompt) > 80 else "")

            items.append(
                ItemCreate(
                    source_type="gemini",
                    source_id=name,
                    title=title or None,
                    content="\n\n".join(parts),
                    created_at=created_at,
                    is_own_content=True,
                    metadata={"source": "takeout", "turn_count": len(turns)},
                )
            )
        except Exception:
            logger.warning(f"Failed to parse Gemini file: {name}", exc_info=True)
            continue

    return items


def _parse_notebooklm_zip(zf, nlm_files, all_names):
    """Parse NotebookLM notebooks from a Google Takeout ZIP.

    Structure: NotebookLM/<notebook-title>/
      - <notebook-title>.json (title, emoji, metadata)
      - Notes/*.html (generated notes)
      - Chat History/*.html (chat sessions)
      - Sources/*.html (source content)
      - Sources/*metadata.json (source metadata)
    """
    import re
    from datetime import datetime

    from lestash.models.item import ItemCreate

    # Discover notebook directories
    nb_dirs: set[str] = set()
    for name in nlm_files:
        match = re.match(r"(Takeout/NotebookLM/[^/]+)/", name)
        if match:
            nb_dirs.add(match.group(1))

    items = []
    for nb_dir in sorted(nb_dirs):
        try:
            nb_name = nb_dir.rsplit("/", 1)[-1]

            # Find metadata JSON (not from Sources/Notes/Chat/Artifacts)
            meta_data: dict = {}
            meta_candidates = [
                n
                for n in all_names
                if n.startswith(f"{nb_dir}/")
                and n.endswith(".json")
                and "/Sources/" not in n
                and "/Notes/" not in n
                and "/Chat History/" not in n
                and "/Artifacts/" not in n
            ]
            for m in meta_candidates:
                try:
                    meta_data = json.loads(zf.read(m))
                    break
                except (json.JSONDecodeError, KeyError):
                    continue

            title = meta_data.get("title", nb_name)
            metadata_inner = meta_data.get("metadata", {})

            # Collect content HTML files
            note_htmls = sorted(
                n for n in all_names if n.startswith(f"{nb_dir}/Notes/") and n.endswith(".html")
            )
            chat_htmls = sorted(
                n
                for n in all_names
                if n.startswith(f"{nb_dir}/Chat History/") and n.endswith(".html")
            )
            source_metas = [
                n for n in all_names if n.startswith(f"{nb_dir}/Sources/") and n.endswith(".json")
            ]

            if not note_htmls and not chat_htmls:
                continue

            created_at = None
            create_time = metadata_inner.get("createTime", "")
            if create_time:
                with contextlib.suppress(ValueError, OSError):
                    created_at = datetime.fromisoformat(str(create_time).replace("Z", "+00:00"))

            source_titles = []
            for sm in source_metas:
                try:
                    sd = json.loads(zf.read(sm))
                    if sd.get("title"):
                        source_titles.append(sd["title"])
                except Exception:
                    continue

            # Parent item: notebook overview
            parent_metadata: dict[str, object] = {
                "source": "takeout",
                "note_count": len(note_htmls),
                "chat_count": len(chat_htmls),
            }
            if meta_data.get("emoji"):
                parent_metadata["emoji"] = meta_data["emoji"]
            if source_titles:
                parent_metadata["sources"] = source_titles
                parent_metadata["source_count"] = len(source_titles)

            summary_parts = [f"# {title}"]
            if source_titles:
                summary_parts.append(
                    f"**Sources ({len(source_titles)}):** " + ", ".join(source_titles)
                )
            summary_parts.append(f"**Notes:** {len(note_htmls)} | **Chats:** {len(chat_htmls)}")

            items.append(
                ItemCreate(
                    source_type="notebooklm",
                    source_id=nb_dir,
                    title=title,
                    content="\n\n".join(summary_parts),
                    created_at=created_at,
                    is_own_content=True,
                    metadata=parent_metadata,
                )
            )

            # Child items: individual notes
            for nh in note_htmls:
                note_name = nh.rsplit("/", 1)[-1].replace(".html", "")
                html = zf.read(nh).decode("utf-8", errors="replace")
                text = re.sub(r"<[^>]+>", "", html).strip()
                if text:
                    items.append(
                        ItemCreate(
                            source_type="notebooklm",
                            source_id=nh,
                            title=f"{title} — {note_name}",
                            content=text,
                            created_at=created_at,
                            is_own_content=True,
                            metadata={
                                "source": "takeout",
                                "type": "note",
                                "_parent_source_id": nb_dir,
                            },
                        )
                    )

            # Child items: individual chat sessions
            for ch in chat_htmls:
                chat_name = ch.rsplit("/", 1)[-1].replace(".html", "")
                html = zf.read(ch).decode("utf-8", errors="replace")
                text = re.sub(r"<[^>]+>", "", html).strip()
                if text:
                    text = text.replace("MODEL:", "**Gemini:**")
                    items.append(
                        ItemCreate(
                            source_type="notebooklm",
                            source_id=ch,
                            title=f"{title} — {chat_name}",
                            content=text,
                            created_at=created_at,
                            is_own_content=True,
                            metadata={
                                "source": "takeout",
                                "type": "chat",
                                "_parent_source_id": nb_dir,
                            },
                        )
                    )
        except Exception:
            logger.warning(f"Failed to parse NotebookLM notebook: {nb_dir}", exc_info=True)
            continue

    return items
