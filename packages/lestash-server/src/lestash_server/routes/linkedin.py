"""LinkedIn posting endpoints."""

import json
import logging
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, UploadFile
from pydantic import BaseModel

from lestash_server.deps import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/linkedin", tags=["linkedin"])


class LinkedInPostRequest(BaseModel):
    """Request body for text and article posts."""

    text: str
    visibility: str = "PUBLIC"
    article_url: str | None = None
    article_title: str | None = None
    article_description: str | None = None


class LinkedInPostResponse(BaseModel):
    """Response after posting."""

    status: str
    post_urn: str


def _get_api():
    """Create a LinkedInAPI instance from stored write credentials."""
    from lestash_linkedin.api import LinkedInAPI, get_person_urn, load_write_token

    token = load_write_token()
    if not token or not token.get("access_token"):
        raise HTTPException(
            status_code=401,
            detail="No LinkedIn posting token. Run: lestash linkedin auth-post",
        )

    person_urn = get_person_urn()
    if not person_urn:
        raise HTTPException(
            status_code=400,
            detail="No person URN configured. Run: lestash linkedin auth-post --person-urn URN",
        )

    api = LinkedInAPI(token["access_token"])
    return api, person_urn


def _save_item(post_urn: str, text: str, visibility: str, metadata_extra: dict | None = None):
    """Save the posted content as a LeStash item."""
    metadata = {
        "post_urn": post_urn,
        "visibility": visibility,
        "resource_name": "ugcPosts",
    }
    if metadata_extra:
        metadata.update(metadata_extra)

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO items (
                source_type, source_id, url, title, content,
                author, created_at, is_own_content, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_type, source_id) DO UPDATE SET
                content = excluded.content,
                metadata = excluded.metadata
            """,
            (
                "linkedin",
                post_urn,
                None,
                None,
                text,
                None,
                datetime.now().isoformat(),
                True,
                json.dumps(metadata),
            ),
        )
        conn.commit()


@router.post("/post", response_model=LinkedInPostResponse, status_code=201)
def create_post(body: LinkedInPostRequest):
    """Create a LinkedIn text or article post."""
    import httpx

    if len(body.text) > 3000:
        raise HTTPException(
            status_code=400,
            detail=f"Text too long: {len(body.text)} chars (max 3,000)",
        )

    if body.visibility not in ("PUBLIC", "CONNECTIONS"):
        raise HTTPException(status_code=400, detail=f"Invalid visibility: {body.visibility}")

    if body.article_url and not body.article_title:
        raise HTTPException(status_code=400, detail="article_title required with article_url")

    api, person_urn = _get_api()
    try:
        post_urn = api.create_post(
            text=body.text,
            author_urn=person_urn,
            visibility=body.visibility,
            article_url=body.article_url,
            article_title=body.article_title,
            article_description=body.article_description,
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            raise HTTPException(
                status_code=403,
                detail="Missing w_member_social scope. "
                "Add 'Share on LinkedIn' product and re-auth.",
            ) from None
        if e.response.status_code == 401:
            raise HTTPException(status_code=401, detail="Token expired. Re-authenticate.") from None
        raise HTTPException(status_code=e.response.status_code, detail=str(e)) from None
    finally:
        api.close()

    extra: dict[str, str] = {}
    if body.article_url:
        extra["article_url"] = body.article_url
        extra["article_title"] = body.article_title or ""
    _save_item(post_urn, body.text, body.visibility, extra or None)

    return LinkedInPostResponse(status="posted", post_urn=post_urn)


@router.post("/post-with-image", response_model=LinkedInPostResponse, status_code=201)
def create_post_with_image(
    image: UploadFile,
    text: str = Form(...),
    visibility: str = Form("PUBLIC"),
):
    """Create a LinkedIn post with an image attachment."""
    import httpx

    if len(text) > 3000:
        raise HTTPException(status_code=400, detail=f"Text too long: {len(text)} chars (max 3,000)")

    if visibility not in ("PUBLIC", "CONNECTIONS"):
        raise HTTPException(status_code=400, detail=f"Invalid visibility: {visibility}")

    # Save uploaded image to temp file
    suffix = Path(image.filename or "image.jpg").suffix or ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(image.file.read())
        tmp_path = Path(tmp.name)

    api, person_urn = _get_api()
    try:
        post_urn = api.create_post(
            text=text,
            author_urn=person_urn,
            visibility=visibility,
            image_path=tmp_path,
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            raise HTTPException(
                status_code=403,
                detail="Missing w_member_social scope. "
                "Add 'Share on LinkedIn' product and re-auth.",
            ) from None
        if e.response.status_code == 401:
            raise HTTPException(status_code=401, detail="Token expired. Re-authenticate.") from None
        raise HTTPException(status_code=e.response.status_code, detail=str(e)) from None
    finally:
        api.close()
        tmp_path.unlink(missing_ok=True)

    _save_item(post_urn, text, visibility, {"has_image": True})

    return LinkedInPostResponse(status="posted", post_urn=post_urn)
