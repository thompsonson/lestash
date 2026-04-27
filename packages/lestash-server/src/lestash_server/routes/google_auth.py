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
        default="https://www.googleapis.com/auth/youtube.readonly,https://www.googleapis.com/auth/drive.readonly",
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


class AndroidConfigResponse(BaseModel):
    """Public OAuth configuration the Android app needs to start auth."""

    web_client_id: str


@router.get("/android-config", response_model=AndroidConfigResponse)
def android_config():
    """Expose the Web OAuth client id so the Android app can pass it to
    AuthorizationClient.requestOfflineAccess(). The client id is public; the
    matching client secret never leaves the server."""
    from lestash.core.google_auth import get_web_client_config

    try:
        web = get_web_client_config()
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return AndroidConfigResponse(web_client_id=web["client_id"])


class AndroidAuthCompleteRequest(BaseModel):
    """Server auth code from Android Identity Services + scopes Google granted."""

    code: str
    granted_scopes: list[str] = []


@router.post("/android-auth-complete", response_model=AuthCompleteResponse)
def complete_android_authentication(body: AndroidAuthCompleteRequest):
    """Exchange a server auth code from Android Identity Services for tokens.

    The Android app obtains the code via AuthorizationClient.authorize() with
    requestOfflineAccess(WEB_CLIENT_ID); we exchange it here using the matching
    Web OAuth client secret. Empty redirect_uri is required for native auth-code
    exchange.
    """
    from google_auth_oauthlib.flow import Flow
    from lestash.core.google_auth import get_web_client_config, save_credentials

    try:
        web = get_web_client_config()
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    scopes = body.granted_scopes or None
    flow = Flow.from_client_config({"web": web}, scopes=scopes, redirect_uri="")

    try:
        flow.fetch_token(code=body.code)
    except Exception as e:
        logger.error(f"Android auth-code exchange failed: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {e}") from e

    credentials = flow.credentials
    final_scopes = list(credentials.scopes) if credentials.scopes else (scopes or [])
    save_credentials(credentials, final_scopes)
    return AuthCompleteResponse(status="ok", scopes=final_scopes)
