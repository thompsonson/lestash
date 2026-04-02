"""Audible authentication API endpoints."""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/audible", tags=["audible"])
logger = logging.getLogger(__name__)

# In-memory state for pending auth flows (keyed by serial)
_pending_auth: dict[str, dict] = {}


class AuthUrlResponse(BaseModel):
    """Response with the Amazon login URL."""

    url: str
    state_token: str


class AuthCompleteRequest(BaseModel):
    """Request to complete auth with the redirect URL."""

    redirect_url: str
    state_token: str


class AuthCompleteResponse(BaseModel):
    """Response after successful authentication."""

    status: str
    locale: str


class AuthStatusResponse(BaseModel):
    """Response with auth status."""

    authenticated: bool
    locale: str | None = None


@router.get("/auth-status", response_model=AuthStatusResponse)
def auth_status():
    """Check if Audible is authenticated."""
    try:
        from lestash_audible.client import is_authenticated, load_config

        config = load_config()
        return AuthStatusResponse(
            authenticated=is_authenticated(),
            locale=config.get("locale"),
        )
    except ImportError:
        return AuthStatusResponse(authenticated=False)


@router.get("/auth-url", response_model=AuthUrlResponse)
def get_auth_url(locale: str = "uk"):
    """Generate Amazon OAuth URL for Audible authentication."""
    try:
        from lestash_audible.client import build_login_url
    except ImportError as e:
        raise HTTPException(
            status_code=501, detail="lestash-audible not installed"
        ) from e

    state = build_login_url(locale=locale)
    state_token = state["serial"]
    _pending_auth[state_token] = state
    return AuthUrlResponse(url=state["url"], state_token=state_token)


@router.post("/auth-complete", response_model=AuthCompleteResponse)
def complete_authentication(body: AuthCompleteRequest):
    """Complete Audible authentication with the redirect URL."""
    try:
        from lestash_audible.client import complete_auth
    except ImportError as e:
        raise HTTPException(
            status_code=501, detail="lestash-audible not installed"
        ) from e

    state = _pending_auth.pop(body.state_token, None)
    if not state:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired state token. Start auth again.",
        )

    try:
        complete_auth(body.redirect_url, state)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Audible auth failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Authentication failed: {e}"
        ) from e

    return AuthCompleteResponse(status="ok", locale=state["locale"])
