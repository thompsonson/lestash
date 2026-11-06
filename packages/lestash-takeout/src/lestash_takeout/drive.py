"""Find and download Takeout zips that Google delivered to Drive (read-only)."""

from pathlib import Path
from typing import Any

# Your own zips only: Drive also lists files others shared with you. No mimeType filter:
# Drive labels Takeout zips "application/x-zip", not "application/zip"; the name is checked.
TAKEOUT_QUERY = "name contains 'takeout-' and 'me' in owners and trashed = false"
CHUNK_SIZE = 64 * 1024 * 1024


def list_takeout_zips(service: Any) -> list[dict[str, Any]]:
    """Takeout zips in Drive, oldest first: [{id, name, size, createdTime}]."""
    files: list[dict[str, Any]] = []
    page_token = None
    while True:
        response = (
            service.files()
            .list(
                q=TAKEOUT_QUERY,
                fields="nextPageToken, files(id, name, size, createdTime)",
                pageSize=200,
                pageToken=page_token,
            )
            .execute()
        )
        files.extend(f for f in response.get("files", []) if f["name"].endswith(".zip"))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return sorted(files, key=lambda f: (f.get("createdTime", ""), f["name"]))


def download(service: Any, file_id: str, dest: Path) -> Path:
    """Stream a Drive file to disk in chunks (Takeout parts can be several GB)."""
    from googleapiclient.http import MediaIoBaseDownload

    request = service.files().get_media(fileId=file_id)
    with dest.open("wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=CHUNK_SIZE)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return dest
