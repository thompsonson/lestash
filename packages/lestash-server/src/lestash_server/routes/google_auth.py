"""Google OAuth web authentication endpoints.

Provides browser-based OAuth flow for Google services (YouTube, Drive, Docs).
Unlike Audible (manual URL paste), Google OAuth redirects back to the server
automatically with the authorization code.
"""

import logging
import secrets

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter(prefix="/api/google", tags=["google-auth"])
logger = logging.getLogger(__name__)

# In-memory state for pending auth flows
_pending_auth: dict[str, dict] = {}


class AuthUrlResponse(BaseModel):
    """Response with the Google OAuth URL."""

    url: str
    state: str


class AuthStatusResponse(BaseModel):
    """Response with Google auth status."""

    authenticated: bool
    scopes: list[str] = []


@router.get("/auth-status", response_model=AuthStatusResponse)
def auth_status():
    """Check if Google is authenticated."""
    try:
        from lestash.core.google_auth import check_auth_status

        status = check_auth_status()
        return AuthStatusResponse(
            authenticated=status.get("authenticated", False),
            scopes=status.get("scopes", []),
        )
    except ImportError:
        return AuthStatusResponse(authenticated=False)


@router.get("/auth-url", response_model=AuthUrlResponse)
def get_auth_url(
    scopes: str = Query(
        default="https://www.googleapis.com/auth/youtube.readonly",
        description="Comma-separated OAuth scopes",
    ),
):
    """Generate Google OAuth URL for browser-based authentication."""
    from lestash.core.google_auth import get_client_secrets_path

    secrets_path = get_client_secrets_path()
    if not secrets_path.exists():
        raise HTTPException(
            status_code=400,
            detail=f"Client secrets not found at {secrets_path}. "
            "Download from Google Cloud Console.",
        )

    from google_auth_oauthlib.flow import InstalledAppFlow

    scope_list = [s.strip() for s in scopes.split(",")]
    state = secrets.token_urlsafe(16)

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), scope_list)
    # Use OOB redirect — user will paste the code
    flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
    auth_url, _ = flow.authorization_url(prompt="consent", state=state)

    _pending_auth[state] = {
        "flow": flow,
        "scopes": scope_list,
    }

    return AuthUrlResponse(url=auth_url, state=state)


class AuthCompleteRequest(BaseModel):
    """Request to complete auth with the authorization code."""

    code: str
    state: str


class AuthCompleteResponse(BaseModel):
    """Response after successful authentication."""

    status: str
    scopes: list[str]


@router.post("/auth-complete", response_model=AuthCompleteResponse)
def complete_authentication(body: AuthCompleteRequest):
    """Complete Google authentication with the authorization code."""
    from lestash.core.google_auth import save_credentials

    pending = _pending_auth.pop(body.state, None)
    if not pending:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired state. Start auth again.",
        )

    flow = pending["flow"]
    try:
        flow.fetch_token(code=body.code)
    except Exception as e:
        logger.error(f"Google auth failed: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {e}") from e

    save_credentials(flow.credentials, pending["scopes"])
    return AuthCompleteResponse(status="ok", scopes=pending["scopes"])
