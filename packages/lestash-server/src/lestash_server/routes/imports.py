"""File import endpoint."""

import json
import logging
import zipfile
from datetime import UTC
from io import BytesIO

from fastapi import APIRouter, HTTPException, UploadFile

from lestash_server.deps import get_db
from lestash_server.models import ImportResponse
from lestash_server.parsers.json_items import parse_json_items

logger = logging.getLogger(__name__)

router = APIRouter(tags=["import"])

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB


@router.post("/api/import", response_model=ImportResponse)
async def import_file(file: UploadFile):
    """Import items from an uploaded file.

    Supports:
    - JSON array of items (.json)
    - ZIP files with known structures (Google Takeout, Claude export)
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
        else:
            # Try JSON first, then fail
            try:
                items = parse_json_items(data)
                source_type = "json"
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported file format: {filename}. Use .json or .zip",
                ) from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None

    # Insert items
    items_added = 0
    items_updated = 0

    with get_db() as conn:
        for item in items:
            try:
                metadata_json = json.dumps(item.metadata) if item.metadata else None
                source_id = item.source_id or item.url
                cursor = conn.execute(
                    """
                    INSERT INTO items (
                        source_type, source_id, url, title, content,
                        author, created_at, is_own_content, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_type, source_id) DO UPDATE SET
                        content = excluded.content,
                        title = excluded.title,
                        author = excluded.author,
                        metadata = excluded.metadata
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
                    ),
                )
                if cursor.rowcount > 0:
                    items_added += 1
            except Exception as e:
                errors.append(f"Failed to import item: {e}")

        conn.commit()

    return ImportResponse(
        status="completed",
        source_type=source_type,
        items_added=items_added,
        items_updated=items_updated,
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

            # Detect Gemini Takeout
            gemini_files = [n for n in names if "Gemini" in n and n.endswith(".json")]
            if gemini_files:
                return _parse_gemini_zip(zf, gemini_files), "gemini"

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
    """Parse Gemini conversations from a Google Takeout ZIP (stub)."""
    raise ValueError(
        "Gemini Takeout import is not yet implemented. "
        "Please export as JSON manually or check issue #33."
    )
