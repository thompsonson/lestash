"""YouTube API endpoints."""

import logging
import re

from fastapi import APIRouter, HTTPException
from lestash.core.database import upsert_item
from pydantic import BaseModel

from lestash_server.deps import get_db

router = APIRouter(prefix="/api/youtube", tags=["youtube"])
logger = logging.getLogger(__name__)


def _extract_video_id(url_or_id: str) -> str | None:
    """Extract YouTube video ID from a URL or bare ID."""
    match = re.search(r"[?&]v=([a-zA-Z0-9_-]{11})", url_or_id)
    if match:
        return match.group(1)
    match = re.search(r"youtu\.be/([a-zA-Z0-9_-]{11})", url_or_id)
    if match:
        return match.group(1)
    if re.match(r"^[a-zA-Z0-9_-]{11}$", url_or_id):
        return url_or_id
    return None


class TranscriptRequest(BaseModel):
    """Request to fetch a YouTube transcript."""

    url: str


class TranscriptResponse(BaseModel):
    """Response after fetching a transcript."""

    item_id: int
    title: str
    word_count: int


@router.post("/fetch-transcript", response_model=TranscriptResponse)
def fetch_transcript(body: TranscriptRequest):
    """Fetch and store a YouTube video transcript."""
    try:
        from lestash_youtube.client import get_transcript
        from lestash_youtube.source import resolve_transcript_parent, transcript_to_item
    except ImportError as e:
        raise HTTPException(status_code=501, detail="lestash-youtube not installed") from e

    video_id = _extract_video_id(body.url)
    if not video_id:
        raise HTTPException(status_code=400, detail="Could not extract video ID from URL")

    transcript = get_transcript(video_id)
    if not transcript:
        raise HTTPException(status_code=404, detail="No transcript available for this video")

    # Try to get video metadata
    video_title = None
    video_author = None
    try:
        from lestash_youtube.client import create_youtube_client, get_video_details

        details = get_video_details(create_youtube_client(), video_id)
        if details:
            video_title = details.get("title")
            video_author = details.get("channel_title")
    except Exception:
        pass

    item = transcript_to_item(video_id, transcript, video_title, video_author)

    with get_db() as conn:
        # Link to the video item if one exists (any subtype, or a share capture).
        parent_id = resolve_transcript_parent(conn, video_id)
        if parent_id:
            item.parent_id = parent_id
        if item.metadata:
            item.metadata.pop("_parent_source_id", None)

        item_id = upsert_item(conn, item)

    word_count = len(transcript["full_text"].split())
    return TranscriptResponse(
        item_id=item_id,
        title=video_title or video_id,
        word_count=word_count,
    )


class ImportVideoRequest(BaseModel):
    """Request to import a YouTube video as a first-class item."""

    url: str
    note: str | None = None


class ImportVideoResponse(BaseModel):
    """Response after importing a video."""

    item_id: int
    title: str
    created: bool  # True if a new item was minted, False if it already existed


@router.post("/import-video", response_model=ImportVideoResponse)
def import_video(body: ImportVideoRequest):
    """Import a YouTube video as a canonical `youtube` item (subtype `shared`).

    Dedup-safe: if a YouTube video item for this id already exists (any
    subtype), returns it instead of creating a duplicate.
    """
    try:
        from lestash_youtube.client import create_youtube_client, get_video_details
        from lestash_youtube.source import find_video_item, video_to_item
    except ImportError as e:
        raise HTTPException(status_code=501, detail="lestash-youtube not installed") from e

    video_id = _extract_video_id(body.url)
    if not video_id:
        raise HTTPException(status_code=400, detail="Could not extract video ID from URL")

    with get_db() as conn:
        # Dedup: reuse an existing video item for this id, regardless of subtype.
        existing_id = find_video_item(conn, video_id)
        if existing_id is not None:
            row = conn.execute("SELECT title FROM items WHERE id = ?", (existing_id,)).fetchone()
            return ImportVideoResponse(
                item_id=existing_id,
                title=(row[0] if row else None) or video_id,
                created=False,
            )

    try:
        details = get_video_details(create_youtube_client(), video_id)
    except Exception as e:
        logger.warning("Failed to fetch video details for %s: %s", video_id, e)
        details = None
    if not details:
        raise HTTPException(status_code=404, detail="Video not found or not accessible")

    item = video_to_item(details, source_subtype="shared", note=body.note)

    with get_db() as conn:
        item_id = upsert_item(conn, item)

    return ImportVideoResponse(
        item_id=item_id,
        title=details.get("title") or video_id,
        created=True,
    )
