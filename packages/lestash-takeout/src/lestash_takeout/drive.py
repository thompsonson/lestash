"""Find and download Takeout zips that Google delivered to Drive (read-only)."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TAKEOUT_QUERY = "name contains 'takeout-' and trashed = false"
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


def get_state_path() -> Path:
    config_dir = Path.home() / ".config" / "lestash"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "takeout_state.json"


def load_state() -> dict[str, Any]:
    """{"imported": {file_id: {"name", "imported_at", "items", "source_type"}}}."""
    path = get_state_path()
    if not path.exists():
        return {"imported": {}}
    try:
        state = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"imported": {}}
    state.setdefault("imported", {})
    return state


def mark_imported(file: dict[str, Any], items: int, source_type: str) -> None:
    state = load_state()
    state["imported"][file["id"]] = {
        "name": file["name"],
        "imported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "items": items,
        "source_type": source_type,
    }
    get_state_path().write_text(json.dumps(state, indent=2))


def forget(file_id: str) -> bool:
    state = load_state()
    removed = state["imported"].pop(file_id, None) is not None
    get_state_path().write_text(json.dumps(state, indent=2))
    return removed
