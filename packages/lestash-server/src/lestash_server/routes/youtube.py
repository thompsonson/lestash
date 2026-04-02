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
        from lestash_youtube.source import transcript_to_item
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
        from lestash_youtube.client import create_youtube_client

        youtube = create_youtube_client()
        response = youtube.videos().list(part="snippet", id=video_id).execute()
        if response.get("items"):
            snippet = response["items"][0]["snippet"]
            video_title = snippet.get("title")
            video_author = snippet.get("channelTitle")
    except Exception:
        pass

    item = transcript_to_item(video_id, transcript, video_title, video_author)

    with get_db() as conn:
        # Link to parent video if it exists
        row = conn.execute(
            "SELECT id FROM items WHERE source_type = 'youtube' AND source_id = ?",
            (f"liked:{video_id}",),
        ).fetchone()
        if row:
            item.parent_id = row[0]
        if item.metadata:
            item.metadata.pop("_parent_source_id", None)

        item_id = upsert_item(conn, item)

    word_count = len(transcript["full_text"].split())
    return TranscriptResponse(
        item_id=item_id,
        title=video_title or video_id,
        word_count=word_count,
    )
