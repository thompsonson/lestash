"""Voice note endpoints — LLM refinement and audio upload."""

import logging
import os
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, UploadFile

from lestash_server.models import RefineRequest, RefineResponse

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

    cache_dir = Path.home() / ".config" / "lestash" / "cache" / "voice"
    cache_dir.mkdir(parents=True, exist_ok=True)

    filename = f"voice-{int(time.time())}-{file.filename or 'recording.wav'}"
    filepath = cache_dir / filename
    filepath.write_bytes(data)

    return {"path": f"voice/{filename}", "size": len(data)}
