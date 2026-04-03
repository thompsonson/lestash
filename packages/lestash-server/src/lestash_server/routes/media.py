"""Media API endpoints."""

import logging
import mimetypes

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from lestash.core.database import (
    add_item_media,
    delete_item_media,
    get_media_dir,
    save_media_file,
)

from lestash_server.deps import get_config, get_db
from lestash_server.models import MediaResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["media"])


@router.get("/api/media/{media_id}")
def serve_media(media_id: int):
    """Serve a media file by ID.

    Returns the local file if available, otherwise redirects to the remote URL.
    """
    with get_db() as conn:
        row = conn.execute("SELECT * FROM item_media WHERE id = ?", (media_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Media not found")

        media = dict(row)

    # Prefer local file
    if media["local_path"]:
        media_dir = get_media_dir(get_config())
        file_path = media_dir / media["local_path"]
        if file_path.is_file():
            content_type = (
                media["mime_type"]
                or mimetypes.guess_type(str(file_path))[0]
                or "application/octet-stream"
            )
            return FileResponse(file_path, media_type=content_type)

    # Fall back to remote URL
    if media["url"]:
        return RedirectResponse(url=media["url"])

    raise HTTPException(status_code=404, detail="Media file not available")


@router.post("/api/items/{item_id}/media", response_model=MediaResponse, status_code=201)
def upload_media(item_id: int, file: UploadFile):
    """Upload a media attachment to an item."""
    with get_db() as conn:
        row = conn.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Item {item_id} not found")

        data = file.file.read()
        filename = file.filename or "upload.bin"
        mime = file.content_type or mimetypes.guess_type(filename)[0]

        rel_path = save_media_file(item_id, data, filename, get_config())

        if mime and mime.startswith("image/"):
            media_type = "image"
        elif mime == "application/pdf":
            media_type = "pdf"
        else:
            media_type = "image"
        media_id = add_item_media(
            conn,
            item_id,
            media_type=media_type,
            local_path=rel_path,
            mime_type=mime,
            source_origin="upload",
        )

    return MediaResponse(
        id=media_id,
        media_type=media_type,
        serve_url=f"/api/media/{media_id}",
    )


@router.delete("/api/media/{media_id}", status_code=204)
def remove_media(media_id: int):
    """Delete a media attachment."""
    with get_db() as conn:
        if not delete_item_media(conn, media_id):
            raise HTTPException(status_code=404, detail="Media not found")
