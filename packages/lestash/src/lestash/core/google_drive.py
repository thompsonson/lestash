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

FOLDER_MIME = "application/vnd.google-apps.folder"

# Google Workspace types that can be exported
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
GOOGLE_SLIDES_MIME = "application/vnd.google-apps.presentation"

EXPORTABLE_MIMES = {
    GOOGLE_DOC_MIME: (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    GOOGLE_SHEET_MIME: ("text/csv", ".csv"),
    GOOGLE_SLIDES_MIME: ("text/plain", ".txt"),
}

# Mime types to skip entirely (no download, no import)
SKIP_MIME_PREFIXES = (
    "image/",
    "audio/",
    "video/",
    "application/octet-stream",
)


def extract_folder_id(url_or_id: str) -> str:
    """Extract a Google Drive folder ID from a URL or return as-is."""
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", url_or_id)
    if match:
        return match.group(1)
    return url_or_id


def extract_file_id(url_or_id: str) -> str:
    """Extract a Google Drive/Docs file ID from a URL or return as-is."""
    # Match /d/<id> in Drive and Docs URLs
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", url_or_id)
    if match:
        return match.group(1)
    # Match id= parameter
    match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url_or_id)
    if match:
        return match.group(1)
    return url_or_id


def classify_drive_url(url: str) -> tuple[str, str]:
    """Classify a Google Drive/Docs URL and extract its ID.

    Returns:
        Tuple of (type, id) where type is 'folder', 'file', or 'unknown'.
    """
    if re.search(r"drive\.google\.com/drive/folders/", url):
        return "folder", extract_folder_id(url)
    if re.search(r"(drive\.google\.com/file/d/|docs\.google\.com/\w+/d/)", url):
        return "file", extract_file_id(url)
    if re.search(r"/d/[a-zA-Z0-9_-]+", url):
        return "file", extract_file_id(url)
    # Bare ID — assume file
    if re.match(r"^[a-zA-Z0-9_-]{10,}$", url):
        return "file", url
    return "unknown", url


def list_drive_folder(
    service,
    folder_id: str,
    since: str | None = None,
    recursive: bool = False,
    _path_prefix: str = "",
) -> list[dict]:
    """List files in a Google Drive folder.

    Args:
        service: Authenticated Drive v3 service object.
        folder_id: Google Drive folder ID.
        since: Optional ISO timestamp — only return files modified after this.
        recursive: If True, recurse into subfolders.

    Returns:
        List of file metadata dicts, each with an added 'folder_path' key.
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
                fields=("nextPageToken,files(id,name,mimeType,size,modifiedTime,webViewLink)"),
                pageSize=100,
                pageToken=page_token,
                orderBy="name",
            )
            .execute()
        )
        for f in response.get("files", []):
            f["folder_path"] = _path_prefix
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    if recursive:
        folders = [f for f in files if f.get("mimeType") == FOLDER_MIME]
        for folder in folders:
            sub_prefix = f"{_path_prefix}/{folder['name']}" if _path_prefix else folder["name"]
            sub_files = list_drive_folder(
                service,
                folder["id"],
                since=since,
                recursive=True,
                _path_prefix=sub_prefix,
            )
            files.extend(sub_files)

    return files


def _safe_filename(file_id: str, file_name: str, ext: str = "") -> str:
    """Create a safe cache filename from a Drive file ID and name."""
    safe_name = file_name.replace("/", "_").replace("\\", "_")
    return f"{file_id}_{safe_name}{ext}"


def _download_file(service, file_id: str, file_name: str) -> Path:
    """Download a file from Google Drive to the cache directory."""
    from googleapiclient.http import MediaIoBaseDownload

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    output_path = CACHE_DIR / _safe_filename(file_id, file_name)

    request = service.files().get_media(fileId=file_id)
    with open(output_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

    return output_path


def _export_google_file(service, file_id: str, file_name: str, mime_type: str) -> Path:
    """Export a Google Workspace file (Docs, Sheets, Slides) to a local file."""
    export_mime, ext = EXPORTABLE_MIMES[mime_type]

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    output_path = CACHE_DIR / _safe_filename(file_id, file_name, ext)

    data = service.files().export(fileId=file_id, mimeType=export_mime).execute()
    output_path.write_bytes(data)

    return output_path


def file_to_item(file_meta: dict, content: str) -> ItemCreate:
    """Convert a Drive file metadata dict + extracted content into an ItemCreate."""
    file_id = file_meta["id"]
    name = file_meta["name"]
    mime_type = file_meta.get("mimeType", "")
    modified = file_meta.get("modifiedTime")
    web_link = file_meta.get("webViewLink")
    folder_path = file_meta.get("folder_path", "")

    # Strip common extensions from title
    title = re.sub(r"\.(docx|pdf|txt|epub|md|csv)$", "", name, flags=re.IGNORECASE).strip()

    created_at = None
    if modified:
        with contextlib.suppress(ValueError, TypeError):
            created_at = datetime.fromisoformat(modified.replace("Z", "+00:00"))

    metadata: dict = {
        "drive_id": file_id,
        "mime_type": mime_type,
        "file_size": int(file_meta.get("size", 0)),
        "drive_modified_time": modified,
        "drive_web_link": web_link,
    }
    if folder_path:
        metadata["folder_path"] = folder_path
        metadata["categories"] = folder_path.split("/")

    return ItemCreate(
        source_type="google-drive",
        source_id=file_id,
        url=web_link or f"https://drive.google.com/file/d/{file_id}/view",
        title=title,
        content=content or f"[{mime_type} file: {name}]",
        is_own_content=True,
        created_at=created_at,
        metadata=metadata,
    )


def _process_file(service, file_meta: dict) -> tuple[ItemCreate, bytes | None, str]:
    """Download/export a single file, convert to markdown, return the
    ItemCreate together with the source PDF bytes (if applicable) and the
    original filename.

    PDF bytes are retained so the caller can persist them as a `source_pdf`
    media row and run the enrichment pipeline. For non-PDF inputs the second
    return value is `None`.
    """
    from lestash.core.text_extract import extract_content

    file_id = file_meta["id"]
    name = file_meta["name"]
    mime_type = file_meta.get("mimeType", "")

    logger.info("Processing %s (%s)", name, mime_type)

    content = ""
    pdf_bytes: bytes | None = None
    try:
        # Google Workspace files need export, not download
        if mime_type in EXPORTABLE_MIMES:
            path = _export_google_file(service, file_id, name, mime_type)
            export_mime, _ = EXPORTABLE_MIMES[mime_type]
        else:
            path = _download_file(service, file_id, name)
            export_mime = mime_type

        try:
            content = extract_content(path, export_mime)
            if export_mime == "application/pdf":
                try:
                    pdf_bytes = path.read_bytes()
                except Exception:
                    logger.exception("Failed to read PDF bytes for %s", name)
        finally:
            path.unlink(missing_ok=True)
    except Exception:
        logger.exception("Failed to download/extract %s", name)

    return file_to_item(file_meta, content), pdf_bytes, name


def _filter_syncable(files: list[dict]) -> list[dict]:
    """Filter out folders and non-document files."""
    return [
        f
        for f in files
        if f.get("mimeType") != FOLDER_MIME
        and not any(f.get("mimeType", "").startswith(p) for p in SKIP_MIME_PREFIXES)
    ]


def sync_drive_folder(
    folder_id: str,
    since: str | None = None,
    recursive: bool = False,
) -> Iterator[tuple[ItemCreate, bytes | None, str]]:
    """Sync files from a Google Drive folder.

    Yields `(ItemCreate, pdf_bytes_or_None, filename)` tuples. PDF bytes are
    surfaced so the caller can attach the source PDF as a media row and
    trigger enrichment after upsert.
    """
    from lestash.core.google_auth import get_drive_service

    folder_id = extract_folder_id(folder_id)
    service = get_drive_service()
    files = list_drive_folder(service, folder_id, since=since, recursive=recursive)
    files = _filter_syncable(files)

    for file_meta in files:
        yield _process_file(service, file_meta)


def sync_single_file(file_id: str) -> tuple[ItemCreate, bytes | None, str]:
    """Download/export a single Drive file.

    Returns `(ItemCreate, pdf_bytes_or_None, filename)`.
    """
    from lestash.core.google_auth import get_drive_service

    service = get_drive_service()
    file_meta = (
        service.files()
        .get(fileId=file_id, fields="id,name,mimeType,size,modifiedTime,webViewLink")
        .execute()
    )
    return _process_file(service, file_meta)
