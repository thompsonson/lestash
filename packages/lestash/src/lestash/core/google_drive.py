"""Google Drive folder sync — list files and import as LeStash items."""

import contextlib
import logging
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from lestash.models.item import ItemCreate

logger = logging.getLogger(__name__)

CACHE_DIR = Path.home() / ".config" / "lestash" / "cache" / "drive"


def extract_folder_id(url_or_id: str) -> str:
    """Extract a Google Drive folder ID from a URL or return as-is."""
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", url_or_id)
    if match:
        return match.group(1)
    return url_or_id


def list_drive_folder(
    service,
    folder_id: str,
    since: str | None = None,
) -> list[dict]:
    """List all files in a Google Drive folder.

    Args:
        service: Authenticated Drive v3 service object.
        folder_id: Google Drive folder ID.
        since: Optional ISO timestamp — only return files modified after this.

    Returns:
        List of file metadata dicts.
    """
    query = f"'{folder_id}' in parents and trashed = false"
    if since:
        query += f" and modifiedTime > '{since}'"

    files: list[dict] = []
    page_token = None

    while True:
        response = (
            service.files()
            .list(
                q=query,
                fields="nextPageToken,files(id,name,mimeType,size,modifiedTime,webViewLink)",
                pageSize=100,
                pageToken=page_token,
                orderBy="name",
            )
            .execute()
        )
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return files


def _download_file(service, file_id: str, file_name: str) -> Path:
    """Download a file from Google Drive to the cache directory."""
    from googleapiclient.http import MediaIoBaseDownload

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    output_path = CACHE_DIR / f"{file_id}_{file_name}"

    request = service.files().get_media(fileId=file_id)
    with open(output_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

    return output_path


def file_to_item(file_meta: dict, content: str) -> ItemCreate:
    """Convert a Drive file metadata dict + extracted content into an ItemCreate."""
    file_id = file_meta["id"]
    name = file_meta["name"]
    mime_type = file_meta.get("mimeType", "")
    modified = file_meta.get("modifiedTime")
    web_link = file_meta.get("webViewLink")

    # Strip common extensions from title
    title = re.sub(r"\.(docx|pdf|txt|epub)$", "", name, flags=re.IGNORECASE).strip()

    created_at = None
    if modified:
        with contextlib.suppress(ValueError, TypeError):
            created_at = datetime.fromisoformat(modified.replace("Z", "+00:00"))

    return ItemCreate(
        source_type="google-drive",
        source_id=file_id,
        url=web_link or f"https://drive.google.com/file/d/{file_id}/view",
        title=title,
        content=content or f"[{mime_type} file: {name}]",
        is_own_content=True,
        created_at=created_at,
        metadata={
            "drive_id": file_id,
            "mime_type": mime_type,
            "file_size": int(file_meta.get("size", 0)),
            "drive_modified_time": modified,
            "drive_web_link": web_link,
        },
    )


def sync_drive_folder(
    folder_id: str,
    since: str | None = None,
) -> Iterator[ItemCreate]:
    """Sync files from a Google Drive folder, yielding ItemCreate objects.

    Downloads each file to a local cache, converts to markdown via Docling,
    and yields an ItemCreate with the full markdown content.

    Args:
        folder_id: Google Drive folder ID or URL.
        since: Optional ISO timestamp for incremental sync.

    Yields:
        ItemCreate objects ready for database upsert.
    """
    from lestash.core.google_auth import get_drive_service
    from lestash.core.text_extract import extract_content

    folder_id = extract_folder_id(folder_id)
    service = get_drive_service()
    files = list_drive_folder(service, folder_id, since=since)

    # Skip folders
    files = [f for f in files if f.get("mimeType") != "application/vnd.google-apps.folder"]

    for file_meta in files:
        file_id = file_meta["id"]
        name = file_meta["name"]
        mime_type = file_meta.get("mimeType", "")

        logger.info("Processing %s (%s)", name, mime_type)

        # Download and extract content
        content = ""
        try:
            path = _download_file(service, file_id, name)
            try:
                content = extract_content(path, mime_type)
            finally:
                # Clean up cached file
                path.unlink(missing_ok=True)
        except Exception:
            logger.exception("Failed to download/extract %s", name)

        yield file_to_item(file_meta, content)
