"""LinkedIn DMA Portability API client."""

import json
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from lestash.core.config import get_config_dir
from lestash.core.logging import get_plugin_logger
from rich.console import Console

console = Console()
logger = get_plugin_logger("linkedin")

# LinkedIn OAuth endpoints
AUTHORIZATION_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
API_BASE_URL = "https://api.linkedin.com/rest"

# Rate limit documentation
RATE_LIMIT_DOCS = "https://learn.microsoft.com/en-us/linkedin/shared/api-guide/concepts/rate-limits"

# DMA Portability scopes for EU members
SCOPE_SELF_SERVE = "r_dma_portability_self_serve"  # Personal use (Member API)
SCOPE_3RD_PARTY = "r_dma_portability_3rd_party"  # App for others (3rd Party API)

# Write scope for posting (Share on LinkedIn) + openid for person URN discovery
SCOPE_WRITE = "w_member_social openid profile"

# Local callback server
REDIRECT_URI = "http://localhost:8338/callback"


def _load_json(path: Path) -> dict[str, Any] | None:
    """Load a JSON file, returning None if it doesn't exist."""
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def _save_json(path: Path, data: dict[str, Any]) -> None:
    """Save data to a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


# --- DMA (read) credentials and token ---


def get_token_path() -> Path:
    """Get path to stored OAuth token (DMA read)."""
    return get_config_dir() / "linkedin_token.json"


def get_credentials_path() -> Path:
    """Get path to LinkedIn API credentials (DMA read)."""
    return get_config_dir() / "linkedin_credentials.json"


def load_credentials() -> dict[str, str] | None:
    """Load LinkedIn DMA API credentials from config."""
    return _load_json(get_credentials_path())


def save_credentials(
    client_id: str,
    client_secret: str,
    mode: str = "3rd-party",
    person_urn: str | None = None,
) -> None:
    """Save LinkedIn DMA API credentials."""
    _save_creds(get_credentials_path(), client_id, client_secret, mode, person_urn)


def load_token() -> dict[str, Any] | None:
    """Load stored OAuth token (DMA read)."""
    return _load_json(get_token_path())


def save_token(token: dict[str, Any]) -> None:
    """Save OAuth token to config (DMA read)."""
    _save_json(get_token_path(), token)


# --- Write (posting) credentials and token ---


def get_write_token_path() -> Path:
    """Get path to stored OAuth token (posting/write)."""
    return get_config_dir() / "linkedin_write_token.json"


def get_write_credentials_path() -> Path:
    """Get path to LinkedIn API credentials (posting/write)."""
    return get_config_dir() / "linkedin_write_credentials.json"


def load_write_credentials() -> dict[str, str] | None:
    """Load LinkedIn posting API credentials from config."""
    return _load_json(get_write_credentials_path())


def save_write_credentials(
    client_id: str,
    client_secret: str,
    person_urn: str | None = None,
) -> None:
    """Save LinkedIn posting API credentials."""
    _save_creds(get_write_credentials_path(), client_id, client_secret, "write", person_urn)


def load_write_token() -> dict[str, Any] | None:
    """Load stored OAuth token (posting/write)."""
    return _load_json(get_write_token_path())


def save_write_token(token: dict[str, Any]) -> None:
    """Save OAuth token to config (posting/write)."""
    _save_json(get_write_token_path(), token)


# --- Shared credential helpers ---


def _save_creds(
    creds_path: Path,
    client_id: str,
    client_secret: str,
    mode: str,
    person_urn: str | None = None,
) -> None:
    """Save credentials to the given path, preserving existing person_urn."""
    data: dict[str, str] = {
        "client_id": client_id,
        "client_secret": client_secret,
        "mode": mode,
    }
    if person_urn is None:
        existing = _load_json(creds_path)
        if existing and "person_urn" in existing:
            data["person_urn"] = existing["person_urn"]
    else:
        data["person_urn"] = person_urn
    _save_json(creds_path, data)
    console.print(f"[green]Credentials saved to {creds_path}[/green]")


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler to capture OAuth callback."""

    authorization_code: str | None = None

    def do_GET(self) -> None:
        """Handle OAuth callback GET request."""
        parsed = urlparse(self.path)
        if parsed.path == "/callback":
            params = parse_qs(parsed.query)
            if "code" in params:
                OAuthCallbackHandler.authorization_code = params["code"][0]
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(b"""
                    <html><body>
                    <h1>Authorization successful!</h1>
                    <p>You can close this window and return to the terminal.</p>
                    </body></html>
                """)
            else:
                error = params.get("error", ["Unknown error"])[0]
                self.send_response(400)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(f"<html><body><h1>Error: {error}</h1></body></html>".encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress HTTP server logs."""
        pass


def _run_oauth_flow(
    client_id: str,
    client_secret: str,
    scope: str,
    redirect_uri: str = REDIRECT_URI,
) -> dict[str, Any]:
    """Run CLI OAuth authorization flow with local callback server.

    Opens browser for user to authorize, captures callback on localhost,
    exchanges code for token.

    Args:
        client_id: LinkedIn app client ID.
        client_secret: LinkedIn app client secret.
        scope: OAuth scope string.
        redirect_uri: OAuth redirect URI (must match app config).

    Returns:
        Token dict with access_token, expires_in, etc.
    """
    import secrets

    logger.debug(f"Starting OAuth flow with scope={scope}")

    state = secrets.token_urlsafe(16)

    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": scope,
    }
    auth_url = f"{AUTHORIZATION_URL}?{'&'.join(f'{k}={v}' for k, v in auth_params.items())}"

    logger.debug(f"Authorization URL built with scope={scope}")

    console.print("[bold]Opening browser for LinkedIn authorization...[/bold]")
    console.print(f"[dim]If browser doesn't open, visit: {auth_url}[/dim]")

    server = HTTPServer(("localhost", 8338), OAuthCallbackHandler)
    server.timeout = 120

    webbrowser.open(auth_url)

    console.print("[dim]Waiting for authorization...[/dim]")
    logger.debug("Waiting for OAuth callback")
    while OAuthCallbackHandler.authorization_code is None:
        server.handle_request()

    code = OAuthCallbackHandler.authorization_code
    OAuthCallbackHandler.authorization_code = None

    logger.debug("Received authorization code, exchanging for token")

    console.print("[dim]Exchanging code for token...[/dim]")
    token = exchange_code_for_token(code, client_id, client_secret, redirect_uri)

    logger.info("OAuth authorization completed successfully")
    console.print("[green]✓ Authorization successful![/green]")

    return token


def exchange_code_for_token(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str = REDIRECT_URI,
) -> dict[str, Any]:
    """Exchange an authorization code for an access token.

    Args:
        code: Authorization code from OAuth callback.
        client_id: LinkedIn app client ID.
        client_secret: LinkedIn app client secret.
        redirect_uri: Must match the redirect_uri used in the auth request.

    Returns:
        Token dict with access_token, expires_in, etc.
    """
    with httpx.Client() as client:
        response = client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        return response.json()


def build_auth_url(client_id: str, scope: str, redirect_uri: str, state: str) -> str:
    """Build a LinkedIn OAuth authorization URL.

    Args:
        client_id: LinkedIn app client ID.
        scope: OAuth scope string.
        redirect_uri: OAuth redirect URI.
        state: CSRF state token.

    Returns:
        Full authorization URL string.
    """
    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": scope,
    }
    return f"{AUTHORIZATION_URL}?{'&'.join(f'{k}={v}' for k, v in auth_params.items())}"


def authorize(client_id: str, client_secret: str, mode: str = "3rd-party") -> dict[str, Any]:
    """Run CLI OAuth authorization flow for DMA Portability API (read).

    Args:
        client_id: LinkedIn DMA app client ID.
        client_secret: LinkedIn DMA app client secret.
        mode: API mode - "self-serve" for personal use, "3rd-party" for apps.

    Returns:
        Token dict with access_token, expires_in, etc.
    """
    scope = SCOPE_SELF_SERVE if mode == "self-serve" else SCOPE_3RD_PARTY
    token = _run_oauth_flow(client_id, client_secret, scope)
    save_token(token)
    return token


def authorize_write(client_id: str, client_secret: str) -> dict[str, Any]:
    """Run CLI OAuth authorization flow for posting (Share on LinkedIn).

    Uses a separate LinkedIn app with the w_member_social scope.
    Also fetches and stores the numeric person URN via /v2/userinfo.

    Args:
        client_id: LinkedIn posting app client ID.
        client_secret: LinkedIn posting app client secret.

    Returns:
        Token dict with access_token, expires_in, etc.
    """
    token = _run_oauth_flow(client_id, client_secret, SCOPE_WRITE)
    save_write_token(token)

    # Fetch and store the numeric person URN
    person_urn = _fetch_person_urn(token["access_token"])
    if person_urn:
        save_write_credentials(client_id, client_secret, person_urn)
        console.print(f"[dim]Person URN: {person_urn}[/dim]")

    return token


def _fetch_person_urn(access_token: str) -> str | None:
    """Fetch the authenticated member's person URN via /v2/userinfo.

    Requires openid scope. Returns urn:li:person:{sub} where sub is
    the numeric member ID needed by the UGC Posts API.
    """
    try:
        response = httpx.get(
            "https://api.linkedin.com/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10.0,
        )
        response.raise_for_status()
        sub = response.json().get("sub")
        if sub:
            logger.info(f"Fetched person URN: urn:li:person:{sub}")
            return f"urn:li:person:{sub}"
    except Exception as e:
        logger.warning(f"Could not fetch person URN: {e}")
    return None


class LinkedInAPI:
    """LinkedIn DMA Portability API client."""

    def __init__(self, access_token: str, pause_for_rate_limiting: bool = True):
        self.access_token = access_token
        self.pause_for_rate_limiting = pause_for_rate_limiting
        self.client = httpx.Client(
            base_url=API_BASE_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "LinkedIn-Version": "202312",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        logger.debug(
            f"LinkedInAPI client initialized (pause_for_rate_limiting={pause_for_rate_limiting})"
        )

    def close(self) -> None:
        """Close the HTTP client."""
        self.client.close()

    def __enter__(self) -> "LinkedInAPI":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _request(
        self, method: str, url: str, max_retries: int = 3, **kwargs: Any
    ) -> httpx.Response:
        """Make HTTP request with rate limit handling.

        Args:
            method: HTTP method (GET, POST, etc.)
            url: Request URL (relative to base_url)
            max_retries: Maximum number of retries on 429 (default 3)
            **kwargs: Additional arguments to pass to httpx.Client.request()

        Returns:
            httpx.Response object

        Raises:
            httpx.HTTPStatusError: On non-2xx responses (including 429 after max retries)
        """
        retries = 0
        while True:
            response = self.client.request(method, url, **kwargs)

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 60))

                # Log all headers for debugging (rate limit info may be undocumented)
                logger.debug(f"429 response headers: {dict(response.headers)}")

                logger.warning(
                    f"Rate limited (429). Retry-After: {retry_after}s. See: {RATE_LIMIT_DOCS}"
                )
                console.print(
                    f"[yellow]Rate limited (429). Retry-After: {retry_after}s[/yellow]\n"
                    f"[dim]See: {RATE_LIMIT_DOCS}[/dim]"
                )

                if self.pause_for_rate_limiting and retries < max_retries:
                    retries += 1
                    logger.info(
                        f"Pausing for {retry_after} seconds (retry {retries}/{max_retries})..."
                    )
                    console.print(
                        f"[dim]Pausing for {retry_after}s (retry {retries}/{max_retries})...[/dim]"
                    )
                    time.sleep(retry_after)
                    continue  # Retry the request
                else:
                    if retries >= max_retries:
                        console.print(
                            f"[red]Max retries ({max_retries}) exceeded. "
                            f"You may have hit your daily quota (resets at midnight UTC).[/red]"
                        )
                    response.raise_for_status()  # Raise HTTPStatusError

            response.raise_for_status()
            return response

    def get_snapshot(
        self,
        domain: str | None = None,
        start: int = 0,
        count: int = 100,
    ) -> dict[str, Any]:
        """Fetch member snapshot data.

        Args:
            domain: Specific domain (e.g., MEMBER_SHARE_INFO, ALL_COMMENTS).
                   If None, returns all domains.
            start: Pagination start index.
            count: Number of items per page.

        Returns:
            API response dict with paging and elements.
        """
        params = {"q": "criteria", "start": start, "count": count}
        if domain:
            params["domain"] = domain

        logger.debug(f"GET /memberSnapshotData domain={domain} start={start} count={count}")
        response = self._request("GET", "/memberSnapshotData", params=params)
        logger.debug(f"Response status: {response.status_code}")
        return response.json()

    def get_all_snapshot_data(self, domain: str) -> list[dict[str, Any]]:
        """Fetch all paginated data for a domain.

        Args:
            domain: The snapshot domain to fetch.

        Returns:
            List of all snapshot data items.
        """
        logger.debug(f"Fetching all snapshot data for domain={domain}")
        all_data: list[dict[str, Any]] = []
        start = 0
        count = 100

        while True:
            result = self.get_snapshot(domain=domain, start=start, count=count)
            elements = result.get("elements", [])

            if not elements:
                logger.debug(f"No elements returned for domain={domain} at start={start}")
                break

            for element in elements:
                snapshot_data = element.get("snapshotData", [])
                all_data.extend(snapshot_data)

            # Check for more pages
            paging = result.get("paging", {})
            total = paging.get("total", 0)

            logger.debug(
                f"Fetched {len(elements)} elements, total={total}, collected={len(all_data)}"
            )

            if start + count >= total:
                break

            start += count

        logger.info(f"Fetched {len(all_data)} total records for domain={domain}")
        return all_data

    def get_changelog(
        self,
        start_time: int | None = None,
        count: int = 10,
    ) -> dict[str, Any]:
        """Fetch member changelog events.

        The Changelog API returns activity tracked after the user consented,
        limited to the past 28 days.

        Args:
            start_time: Epoch milliseconds (inclusive). Returns events after this time.
            count: Number of events to return (1-50, default 10).

        Returns:
            API response with changelog elements.
        """
        params: dict[str, Any] = {"q": "memberAndApplication", "count": count}
        if start_time is not None:
            params["startTime"] = start_time

        logger.debug(f"GET /memberChangeLogs start_time={start_time} count={count}")
        response = self._request("GET", "/memberChangeLogs", params=params)
        logger.debug(f"Response status: {response.status_code}")
        return response.json()

    def get_all_changelog(self, since_time: int | None = None) -> list[dict[str, Any]]:
        """Fetch all changelog events with pagination.

        Args:
            since_time: Epoch milliseconds. If None, fetches from earliest available.

        Returns:
            List of all changelog events.
        """
        logger.debug(f"Fetching all changelog events since={since_time}")
        all_events: list[dict[str, Any]] = []
        start_time = since_time
        count = 50  # Max allowed

        while True:
            result = self.get_changelog(start_time=start_time, count=count)
            elements = result.get("elements", [])

            if not elements:
                logger.debug("No more changelog events")
                break

            all_events.extend(elements)
            logger.debug(f"Fetched {len(elements)} changelog events, total={len(all_events)}")

            # Use the latest processedAt as the next start_time
            last_event = elements[-1]
            next_time = last_event.get("processedAt")
            if next_time is None or next_time == start_time:
                break
            start_time = next_time

            # Safety check - if we got fewer than requested, we're at the end
            if len(elements) < count:
                break

        logger.info(f"Fetched {len(all_events)} total changelog events")
        return all_events

    def _upload_image(self, image_path: Path, owner_urn: str) -> str:
        """Upload an image for use in a UGC post.

        Uses the legacy v2/assets API (required by Share on LinkedIn product).

        Args:
            image_path: Path to image file (JPG, PNG, or GIF).
            owner_urn: Person URN (e.g. urn:li:person:abc123).

        Returns:
            Asset URN string (e.g. urn:li:digitalmediaAsset:...).
        """
        # Step 1: Register upload
        logger.debug(f"Registering image upload for {image_path.name}")
        register_body = {
            "registerUploadRequest": {
                "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
                "owner": owner_urn,
                "serviceRelationships": [
                    {
                        "relationshipType": "OWNER",
                        "identifier": "urn:li:userGeneratedContent",
                    }
                ],
            }
        }
        # Use v2 API directly (not /rest)
        register_response = httpx.post(
            "https://api.linkedin.com/v2/assets?action=registerUpload",
            json=register_body,
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "X-Restli-Protocol-Version": "2.0.0",
            },
            timeout=30.0,
        )
        register_response.raise_for_status()
        register_data = register_response.json()

        upload_url = register_data["value"]["uploadMechanism"][
            "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"
        ]["uploadUrl"]
        asset_urn = register_data["value"]["asset"]

        # Step 2: Upload binary
        logger.debug(f"Uploading image binary to {upload_url[:80]}...")
        image_bytes = image_path.read_bytes()
        upload_response = httpx.post(
            upload_url,
            content=image_bytes,
            headers={
                "Authorization": f"Bearer {self.access_token}",
            },
            timeout=60.0,
        )
        upload_response.raise_for_status()

        logger.info(f"Image uploaded: {asset_urn}")
        return asset_urn

    def create_post(
        self,
        text: str,
        author_urn: str,
        *,
        visibility: str = "PUBLIC",
        image_path: Path | None = None,
        article_url: str | None = None,
        article_title: str | None = None,
        article_description: str | None = None,
    ) -> str:
        """Create a LinkedIn post via the UGC Posts API.

        Uses the legacy v2/ugcPosts API required by the "Share on LinkedIn"
        product (w_member_social scope). Supports text, image, and article posts.

        Args:
            text: Post text, max 3,000 characters.
            author_urn: Person URN (e.g. urn:li:person:abc123).
            visibility: "PUBLIC" or "CONNECTIONS".
            image_path: Optional image to attach (JPG, PNG, GIF).
            article_url: Optional article URL to share.
            article_title: Article title (used with article_url).
            article_description: Optional article description.

        Returns:
            Post URN string from x-restli-id response header.
        """
        # Determine media category and build media list
        if image_path:
            asset_urn = self._upload_image(image_path, author_urn)
            share_media_category = "IMAGE"
            media = [
                {
                    "status": "READY",
                    "media": asset_urn,
                    "title": {"text": image_path.stem},
                }
            ]
        elif article_url:
            share_media_category = "ARTICLE"
            media_entry: dict[str, Any] = {
                "status": "READY",
                "originalUrl": article_url,
            }
            if article_title:
                media_entry["title"] = {"text": article_title}
            if article_description:
                media_entry["description"] = {"text": article_description}
            media = [media_entry]
        else:
            share_media_category = "NONE"
            media = []

        share_content: dict[str, Any] = {
            "shareCommentary": {"text": text},
            "shareMediaCategory": share_media_category,
        }
        if media:
            share_content["media"] = media

        body: dict[str, Any] = {
            "author": author_urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": share_content,
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": visibility,
            },
        }

        logger.debug(
            f"Creating UGC post (visibility={visibility}, category={share_media_category})"
        )

        # Use v2 API directly (not /rest which requires Marketing API versions)
        response = httpx.post(
            "https://api.linkedin.com/v2/ugcPosts",
            json=body,
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "X-Restli-Protocol-Version": "2.0.0",
            },
            timeout=30.0,
        )
        response.raise_for_status()

        post_urn = response.headers.get("x-restli-id", "")
        logger.info(f"Post created: {post_urn}")
        return post_urn


def get_person_urn(credentials: dict[str, str] | None = None) -> str | None:
    """Get the person URN from stored credentials.

    Checks write credentials first (posting app), then DMA credentials.
    """
    if credentials:
        return credentials.get("person_urn")
    # Write credentials take priority (posting requires person URN)
    write_creds = load_write_credentials()
    if write_creds and "person_urn" in write_creds:
        return write_creds["person_urn"]
    read_creds = load_credentials()
    if read_creds and "person_urn" in read_creds:
        return read_creds["person_urn"]
    return None


# Common snapshot domains that are typically available
# Note: Posts, comments, and likes are NOT in Snapshot API - use Changelog API
SNAPSHOT_DOMAINS = [
    "PROFILE",  # Profile information
    "ARTICLES",  # Long-form articles
    "CONNECTIONS",  # Your connections
    "INBOX",  # Messages
    "POSITIONS",  # Work experience
    "EDUCATION",  # Education history
]
