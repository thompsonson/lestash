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

# Write scope for posting (Community Management / Share on LinkedIn)
SCOPE_WRITE = "w_member_social"

# Local callback server
REDIRECT_URI = "http://localhost:8338/callback"


def get_token_path() -> Path:
    """Get path to stored OAuth token."""
    return get_config_dir() / "linkedin_token.json"


def get_credentials_path() -> Path:
    """Get path to LinkedIn API credentials."""
    return get_config_dir() / "linkedin_credentials.json"


def load_credentials() -> dict[str, str] | None:
    """Load LinkedIn API credentials from config."""
    creds_path = get_credentials_path()
    if creds_path.exists():
        with open(creds_path) as f:
            return json.load(f)
    return None


def save_credentials(
    client_id: str,
    client_secret: str,
    mode: str = "3rd-party",
    person_urn: str | None = None,
) -> None:
    """Save LinkedIn API credentials."""
    creds_path = get_credentials_path()
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, str] = {
        "client_id": client_id,
        "client_secret": client_secret,
        "mode": mode,
    }
    # Preserve existing person_urn if not provided
    if person_urn is None:
        existing = load_credentials()
        if existing and "person_urn" in existing:
            data["person_urn"] = existing["person_urn"]
    else:
        data["person_urn"] = person_urn
    with open(creds_path, "w") as f:
        json.dump(data, f)
    console.print(f"[green]Credentials saved to {creds_path}[/green]")


def load_token() -> dict[str, Any] | None:
    """Load stored OAuth token."""
    token_path = get_token_path()
    if token_path.exists():
        with open(token_path) as f:
            return json.load(f)
    return None


def save_token(token: dict[str, Any]) -> None:
    """Save OAuth token to config."""
    token_path = get_token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    with open(token_path, "w") as f:
        json.dump(token, f)


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


def authorize(client_id: str, client_secret: str, mode: str = "3rd-party") -> dict[str, Any]:
    """Run OAuth authorization flow.

    Opens browser for user to authorize, captures callback,
    exchanges code for token.

    Args:
        client_id: LinkedIn app client ID.
        client_secret: LinkedIn app client secret.
        mode: API mode - "self-serve" for personal use, "3rd-party" for apps.

    Returns:
        Token dict with access_token, expires_in, etc.
    """
    import secrets

    logger.debug(f"Starting OAuth flow with mode={mode}")

    state = secrets.token_urlsafe(16)
    dma_scope = SCOPE_SELF_SERVE if mode == "self-serve" else SCOPE_3RD_PARTY
    scope = f"{dma_scope} {SCOPE_WRITE}"

    # Build authorization URL
    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "scope": scope,
    }
    auth_url = f"{AUTHORIZATION_URL}?{'&'.join(f'{k}={v}' for k, v in auth_params.items())}"

    logger.debug(f"Authorization URL built with scope={scope}")

    console.print("[bold]Opening browser for LinkedIn authorization...[/bold]")
    console.print(f"[dim]If browser doesn't open, visit: {auth_url}[/dim]")

    # Start local server to capture callback
    server = HTTPServer(("localhost", 8338), OAuthCallbackHandler)
    server.timeout = 120  # 2 minute timeout

    # Open browser
    webbrowser.open(auth_url)

    # Wait for callback
    console.print("[dim]Waiting for authorization...[/dim]")
    logger.debug("Waiting for OAuth callback")
    while OAuthCallbackHandler.authorization_code is None:
        server.handle_request()

    code = OAuthCallbackHandler.authorization_code
    OAuthCallbackHandler.authorization_code = None  # Reset for next use

    logger.debug("Received authorization code, exchanging for token")

    # Exchange code for token
    console.print("[dim]Exchanging code for token...[/dim]")
    with httpx.Client() as client:
        response = client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        token = response.json()

    save_token(token)
    logger.info("OAuth authorization completed successfully")
    console.print("[green]✓ Authorization successful![/green]")

    return token


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

    def _posts_headers(self) -> dict[str, str]:
        """Return override headers required by the Posts API.

        The Posts API requires a current-month LinkedIn-Version and
        X-Restli-Protocol-Version header, different from the DMA API defaults.
        """
        from datetime import UTC, datetime

        version = datetime.now(UTC).strftime("%Y%m")
        return {
            "LinkedIn-Version": version,
            "X-Restli-Protocol-Version": "2.0.0",
        }

    def _upload_image(self, image_path: Path, owner_urn: str) -> str:
        """Upload an image for use in a post.

        Three-step flow: initialize upload, PUT binary, return image URN.

        Args:
            image_path: Path to image file (JPG, PNG, or GIF).
            owner_urn: Person URN (e.g. urn:li:person:abc123).

        Returns:
            Image URN string (e.g. urn:li:image:...).
        """
        headers = self._posts_headers()

        # Step 1: Initialize upload
        logger.debug(f"Initializing image upload for {image_path.name}")
        init_response = self._request(
            "POST",
            "/images?action=initializeUpload",
            json={"initializeUploadRequest": {"owner": owner_urn}},
            headers=headers,
        )
        init_data = init_response.json()
        upload_url = init_data["value"]["uploadUrl"]
        image_urn = init_data["value"]["image"]

        # Step 2: Upload binary (external URL, use httpx directly)
        logger.debug(f"Uploading image binary to {upload_url[:80]}...")
        image_bytes = image_path.read_bytes()
        with httpx.Client(timeout=60.0) as upload_client:
            upload_response = upload_client.put(
                upload_url,
                content=image_bytes,
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/octet-stream",
                },
            )
            upload_response.raise_for_status()

        logger.info(f"Image uploaded: {image_urn}")
        return image_urn

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
        """Create a LinkedIn post.

        Supports text-only, image, and article/link share posts.

        Args:
            text: Post text (commentary), max 3,000 characters.
            author_urn: Person URN (e.g. urn:li:person:abc123).
            visibility: "PUBLIC" or "CONNECTIONS".
            image_path: Optional image to attach (JPG, PNG, GIF).
            article_url: Optional article URL to share.
            article_title: Article title (required if article_url is set).
            article_description: Optional article description.

        Returns:
            Post URN string from x-restli-id response header.
        """
        body: dict[str, Any] = {
            "author": author_urn,
            "commentary": text,
            "visibility": visibility,
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }

        # Image post
        if image_path:
            image_urn = self._upload_image(image_path, author_urn)
            body["content"] = {
                "media": {
                    "id": image_urn,
                    "altText": image_path.stem,
                },
            }

        # Article/link share post
        elif article_url:
            article: dict[str, str] = {
                "source": article_url,
                "title": article_title or article_url,
            }
            if article_description:
                article["description"] = article_description
            body["content"] = {"article": article}

        logger.debug(f"Creating post (visibility={visibility}, has_image={image_path is not None})")
        response = self._request(
            "POST",
            "/posts",
            json=body,
            headers=self._posts_headers(),
        )

        post_urn = response.headers.get("x-restli-id", "")
        logger.info(f"Post created: {post_urn}")
        return post_urn


def get_person_urn(credentials: dict[str, str] | None = None) -> str | None:
    """Get the person URN from stored credentials."""
    creds = credentials or load_credentials()
    if creds:
        return creds.get("person_urn")
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
