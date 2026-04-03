"""LinkedIn posting and authentication endpoints."""

import json
import logging
import secrets
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from lestash_server.deps import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/linkedin", tags=["linkedin"])

# In-memory state for pending auth flows
_pending_auth: dict[str, dict] = {}


# --- Auth models ---


class AuthStatusResponse(BaseModel):
    """Response with LinkedIn posting auth status."""

    authenticated: bool
    person_urn: str | None = None


class AuthUrlResponse(BaseModel):
    """Response with the LinkedIn OAuth URL."""

    url: str
    state: str


# --- Auth endpoints ---


@router.get("/auth-status", response_model=AuthStatusResponse)
def auth_status():
    """Check if LinkedIn posting is authenticated."""
    try:
        from lestash_linkedin.api import get_person_urn, load_write_token

        token = load_write_token()
        authenticated = bool(token and token.get("access_token"))
        return AuthStatusResponse(
            authenticated=authenticated,
            person_urn=get_person_urn() if authenticated else None,
        )
    except ImportError:
        return AuthStatusResponse(authenticated=False)


@router.get("/auth-url", response_model=AuthUrlResponse)
def get_auth_url(
    redirect_uri: str = Query(..., description="Server callback URL for OAuth redirect"),
):
    """Generate LinkedIn OAuth URL for posting authentication."""
    from lestash_linkedin.api import (
        SCOPE_WRITE,
        build_auth_url,
        load_write_credentials,
    )

    creds = load_write_credentials()
    if not creds:
        raise HTTPException(
            status_code=400,
            detail="No write credentials configured. "
            "Run: lestash linkedin auth-post --client-id ID --client-secret SECRET",
        )

    state = secrets.token_urlsafe(16)
    url = build_auth_url(creds["client_id"], SCOPE_WRITE, redirect_uri, state)

    _pending_auth[state] = {
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "redirect_uri": redirect_uri,
    }

    return AuthUrlResponse(url=url, state=state)


@router.get("/auth-callback", response_class=HTMLResponse)
def auth_callback(
    code: str = Query(...),
    state: str = Query(...),
):
    """Handle LinkedIn OAuth callback — exchanges code for token."""
    from lestash_linkedin.api import (
        _fetch_person_urn,
        exchange_code_for_token,
        save_write_credentials,
        save_write_token,
    )

    pending = _pending_auth.pop(state, None)
    if not pending:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired state. Start auth again.",
        )

    try:
        token = exchange_code_for_token(
            code,
            pending["client_id"],
            pending["client_secret"],
            pending["redirect_uri"],
        )
        save_write_token(token)

        # Auto-discover numeric person URN
        person_urn = _fetch_person_urn(token["access_token"])
        if person_urn:
            save_write_credentials(pending["client_id"], pending["client_secret"], person_urn)
    except Exception as e:
        logger.error(f"LinkedIn auth failed: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {e}") from e

    return HTMLResponse(
        "<html><body>"
        "<h1>LinkedIn authorization successful!</h1>"
        "<p>You can close this window.</p>"
        "</body></html>"
    )


# --- Post models ---


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


def _save_item(
    post_urn: str, text: str, visibility: str, metadata_extra: dict | None = None
) -> int:
    """Save the posted content as a LeStash item. Returns the item ID."""
    metadata = {
        "post_urn": post_urn,
        "visibility": visibility,
        "resource_name": "ugcPosts",
    }
    if metadata_extra:
        metadata.update(metadata_extra)

    with get_db() as conn:
        cursor = conn.execute(
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
        item_id = cursor.lastrowid
        if not item_id:
            row = conn.execute(
                "SELECT id FROM items WHERE source_type = ? AND source_id = ?",
                ("linkedin", post_urn),
            ).fetchone()
            item_id = row[0]
    return item_id


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
    item_id = _save_item(post_urn, body.text, body.visibility, extra or None)

    # Save article link as media attachment
    if body.article_url:
        from lestash.core.database import add_item_media

        with get_db() as conn:
            add_item_media(
                conn,
                item_id,
                media_type="link",
                url=body.article_url,
                alt_text=body.article_title,
                source_origin="upload",
            )

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
    image_bytes = image.file.read()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(image_bytes)
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

    item_id = _save_item(post_urn, text, visibility, {"has_image": True})

    # Save image locally and create media attachment
    from lestash.core.database import add_item_media, save_media_file

    from lestash_server.deps import get_config

    filename = image.filename or f"image{suffix}"
    rel_path = save_media_file(item_id, image_bytes, filename, get_config())
    with get_db() as conn:
        add_item_media(
            conn,
            item_id,
            media_type="image",
            local_path=rel_path,
            mime_type=image.content_type,
            source_origin="upload",
        )

    return LinkedInPostResponse(status="posted", post_urn=post_urn)
