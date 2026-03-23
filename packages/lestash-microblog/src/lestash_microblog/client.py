"""Micropub API client wrapper for Micro.blog."""

import json
from pathlib import Path
from typing import Any

import httpx

MICROPUB_ENDPOINT = "https://micro.blog/micropub"
MICROBLOG_API_BASE = "https://micro.blog"


def get_config_dir() -> Path:
    """Get lestash config directory."""
    config_dir = Path.home() / ".config" / "lestash"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_token_path() -> Path:
    """Get path to Micro.blog token file."""
    return get_config_dir() / "microblog_token.json"


def save_token(token: str) -> None:
    """Save Micro.blog API token to config file."""
    token_data = {"token": token}

    path = get_token_path()
    path.write_text(json.dumps(token_data, indent=2))
    path.chmod(0o600)  # Make file readable only by owner


def load_token() -> str | None:
    """Load Micro.blog API token from config file."""
    path = get_token_path()
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text())
        return data.get("token")
    except (json.JSONDecodeError, OSError):
        return None


def delete_token() -> bool:
    """Delete stored token. Returns True if deleted, False if not found."""
    path = get_token_path()
    if path.exists():
        path.unlink()
        return True
    return False


class MicropubClient:
    """Client for interacting with Micro.blog's Micropub API."""

    def __init__(self, token: str | None = None, endpoint: str = MICROPUB_ENDPOINT):
        """Initialize the Micropub client.

        Args:
            token: API token. If not provided, loads from config.
            endpoint: Micropub endpoint URL.

        Raises:
            ValueError: If no token provided and none found in config.
        """
        self.token = token or load_token()
        if not self.token:
            raise ValueError(
                "No token provided or found. Run 'lestash microblog auth' first or provide a token."
            )

        self.endpoint = endpoint
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
            timeout=30.0,
        )

    def __enter__(self) -> "MicropubClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self._client.close()

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        """Make a GET request to the Micropub endpoint.

        Args:
            params: Query parameters.

        Returns:
            Response JSON data.

        Raises:
            httpx.HTTPStatusError: If the request fails.
        """
        response = self._client.get(self.endpoint, params=params)
        response.raise_for_status()
        return response.json()

    def get_config(self) -> dict[str, Any]:
        """Get Micropub configuration.

        Returns:
            Configuration dict with media-endpoint, destination, etc.
        """
        return self._request({"q": "config"})

    def get_destinations(self) -> list[dict[str, str]]:
        """Get available blog destinations.

        Returns:
            List of destination dicts with 'uid' and 'name' keys.
        """
        config = self.get_config()
        return config.get("destination", [])

    def get_posts(
        self,
        limit: int = 20,
        offset: int = 0,
        destination: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch posts from Micro.blog.

        Args:
            limit: Maximum number of posts to fetch per request.
            offset: Number of posts to skip (for pagination).
            destination: Optional destination UID for multi-blog accounts.

        Returns:
            List of h-entry posts.
        """
        params: dict[str, Any] = {
            "q": "source",
            "limit": limit,
            "offset": offset,
        }

        if destination:
            params["mp-destination"] = destination

        result = self._request(params)
        return result.get("items", [])

    def get_all_posts(
        self,
        limit: int = 100,
        destination: str | None = None,
        max_posts: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all posts from Micro.blog with pagination.

        Args:
            limit: Number of posts per request.
            destination: Optional destination UID for multi-blog accounts.
            max_posts: Maximum total posts to fetch (None for all).

        Returns:
            List of all h-entry posts.
        """
        all_posts: list[dict[str, Any]] = []
        offset = 0

        while True:
            posts = self.get_posts(limit=limit, offset=offset, destination=destination)

            if not posts:
                break

            all_posts.extend(posts)

            # Check if we've reached max_posts
            if max_posts is not None and len(all_posts) >= max_posts:
                all_posts = all_posts[:max_posts]
                break

            # If we got fewer than requested, we've reached the end
            if len(posts) < limit:
                break

            offset += limit

        return all_posts

    def _api_get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make a GET request to the Micro.blog JSON Feed API.

        Args:
            path: API path (e.g., "/posts/mentions").
            params: Optional query parameters.

        Returns:
            Response JSON data.

        Raises:
            httpx.HTTPStatusError: If the request fails.
        """
        url = f"{MICROBLOG_API_BASE}{path}"
        response = self._client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    def get_mentions(
        self,
        count: int = 20,
        before_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch mentions (replies from others) via JSON Feed API.

        Args:
            count: Maximum number of items to fetch.
            before_id: Fetch items before this ID (for pagination).

        Returns:
            List of JSON Feed items.
        """
        params: dict[str, Any] = {"count": count}
        if before_id is not None:
            params["before_id"] = before_id

        result = self._api_get("/posts/mentions", params)
        return result.get("items", [])

    def get_all_mentions(
        self,
        count: int = 100,
        max_items: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all mentions with cursor-based pagination.

        Args:
            count: Number of items per request.
            max_items: Maximum total items to fetch (None for all).

        Returns:
            List of all JSON Feed mention items.
        """
        all_items: list[dict[str, Any]] = []

        before_id = None
        while True:
            items = self.get_mentions(count=count, before_id=before_id)
            if not items:
                break

            all_items.extend(items)

            if max_items is not None and len(all_items) >= max_items:
                all_items = all_items[:max_items]
                break

            if len(items) < count:
                break

            # Use the last item's _microblog.id for cursor
            last = items[-1]
            last_id = last.get("_microblog", {}).get("id") or last.get("id")
            if last_id is None:
                break
            before_id = int(last_id)

        return all_items

    def get_conversation(self, post_id: str) -> list[dict[str, Any]]:
        """Fetch a full conversation thread via JSON Feed API.

        Args:
            post_id: Micro.blog post ID.

        Returns:
            List of JSON Feed items in the thread.
        """
        result = self._api_get("/posts/conversation", {"id": post_id})
        return result.get("items", [])

    def verify_token(self) -> dict[str, Any]:
        """Verify the token is valid by fetching config.

        Returns:
            Configuration dict if token is valid.

        Raises:
            httpx.HTTPStatusError: If token is invalid.
        """
        return self.get_config()


def create_client(token: str | None = None) -> MicropubClient:
    """Create a Micropub client.

    Args:
        token: API token. If not provided, loads from config.

    Returns:
        Configured MicropubClient.

    Raises:
        ValueError: If no token provided and none found in config.
    """
    return MicropubClient(token=token)
