"""Voice note endpoints — LLM refinement, audio upload, and transcription."""

import asyncio
import logging
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, Form, HTTPException, UploadFile
from lestash.core import paths

from lestash_server.models import RefineRequest, RefineResponse, TranscribeResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/voice", tags=["voice"])

DEFAULT_PROMPT = (
    "Clean up this voice note transcript. Fix grammar, remove filler words "
    "(um, uh, like, you know), improve punctuation, and organize into clear "
    "paragraphs. Maintain the original meaning and tone. Return only the "
    "cleaned text, no commentary."
)


def _get_llm_url() -> str:
    return os.environ.get("LESTASH_LLM_URL", "http://localhost:4000")


@router.post("/refine", response_model=RefineResponse)
def refine_transcript(body: RefineRequest):
    """Refine a transcript using an LLM via OpenAI-compatible API."""
    llm_url = _get_llm_url()
    prompt = body.prompt or DEFAULT_PROMPT
    model = body.model or os.environ.get("LESTASH_LLM_MODEL", "claude-sonnet-4-20250514")

    try:
        resp = httpx.post(
            f"{llm_url}/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": body.text},
                ],
                "temperature": 0.3,
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()

        refined = data["choices"][0]["message"]["content"]
        model_used = data.get("model", model)

        return RefineResponse(
            refined_text=refined,
            model_used=model_used,
            prompt_used=prompt,
        )
    except httpx.ConnectError:
        raise HTTPException(
            status_code=503,
            detail=f"LLM proxy not reachable at {llm_url}. "
            "Configure LESTASH_LLM_URL or start LiteLLM proxy.",
        ) from None
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=502,
            detail=f"LLM proxy error: {e.response.status_code} {e.response.text[:200]}",
        ) from None
    except (KeyError, IndexError) as e:
        raise HTTPException(
            status_code=502,
            detail=f"Unexpected LLM response format: {e}",
        ) from None


@router.post("/upload")
async def upload_audio(file: UploadFile):
    """Upload raw audio file to cache directory."""
    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large (max 50MB)")

    cache_dir = paths.voice_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    filename = f"voice-{int(time.time())}-{file.filename or 'recording.wav'}"
    filepath = cache_dir / filename
    filepath.write_bytes(data)

    return {"path": f"voice/{filename}", "size": len(data)}


SUPPORTED_EXTENSIONS = {".m4a", ".mp3", ".wav", ".ogg", ".flac", ".webm"}


@router.post("/transcribe", response_model=TranscribeResponse, status_code=201)
async def transcribe_audio(
    file: UploadFile,
    model: str = Form("base.en"),
    title: str | None = Form(None),
    parent_id: int | None = Form(None),
):
    """Upload an audio file, transcribe via Whisper, and save as a voice note.

    Accepts m4a, mp3, wav, ogg, flac, or webm files (max 50MB). With ``parent_id``
    the note goes under that item (#114). If the item is deleted while the audio is
    transcribed, the note is saved on its own, and the response's ``parent_id`` is None.
    """
    from lestash.core.database import upsert_item
    from lestash.models.item import ItemCreate
    from lestash_voice.transcribe import transcribe_file

    from lestash_server.deps import get_db

    # Validate file extension
    original_filename = file.filename or "recording.wav"
    ext = Path(original_filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format: {ext}. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}",
        )

    # Read and size-check
    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large (max 50MB)")

    if parent_id is not None:
        with get_db() as conn:
            if not _item_exists(conn, parent_id):
                raise HTTPException(status_code=404, detail=f"Item {parent_id} not found")

    # Save to temp file for transcription
    cache_dir = paths.voice_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    temp_filename = f"voice-{int(time.time())}-{original_filename}"
    temp_path = cache_dir / temp_filename
    temp_path.write_bytes(data)

    try:
        result = await asyncio.to_thread(transcribe_file, str(temp_path), model_name=model)
    except Exception as e:
        logger.exception("Transcription failed for %s", original_filename)
        raise HTTPException(status_code=500, detail=f"Transcription failed: {e}") from None
    finally:
        temp_path.unlink(missing_ok=True)

    if not result.text:
        raise HTTPException(status_code=422, detail="No speech detected in audio")

    # Save as item
    source_id = f"voice-file-{uuid.uuid4().hex[:8]}-{Path(original_filename).stem}"
    note_title = title or Path(original_filename).stem
    item = ItemCreate(
        source_type="voice",
        source_id=source_id,
        title=note_title,
        content=result.text,
        created_at=datetime.now(UTC),
        is_own_content=True,
        metadata={
            "duration_seconds": result.duration_seconds,
            "model": result.model,
            "language": result.language,
            "original_filename": original_filename,
            "input_type": "file",
        },
    )

    with get_db() as conn:
        # Transcribing takes a while: the item may have been deleted since
        if parent_id is not None and _item_exists(conn, parent_id):
            item.parent_id = parent_id
        item_id = upsert_item(conn, item)

    return TranscribeResponse(
        text=result.text,
        language=result.language,
        duration_seconds=result.duration_seconds,
        model=result.model,
        item_id=item_id,
        title=note_title,
        parent_id=item.parent_id,
    )


def _item_exists(conn, item_id: int) -> bool:
    return conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone() is not None
