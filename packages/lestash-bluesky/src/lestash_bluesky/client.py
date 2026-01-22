"""AT Protocol client wrapper for Bluesky."""

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from atproto import Client, models


def get_config_dir() -> Path:
    """Get lestash config directory."""
    config_dir = Path.home() / ".config" / "lestash"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_credentials_path() -> Path:
    """Get path to Bluesky credentials file."""
    return get_config_dir() / "bluesky_credentials.json"


def get_session_path() -> Path:
    """Get path to Bluesky session file."""
    return get_config_dir() / "bluesky_session.json"


def save_credentials(handle: str, password: str) -> None:
    """Save Bluesky credentials to config file."""
    credentials = {
        "handle": handle,
        "password": password,
    }

    path = get_credentials_path()
    path.write_text(json.dumps(credentials, indent=2))
    path.chmod(0o600)  # Make file readable only by owner


def load_credentials() -> dict[str, str] | None:
    """Load Bluesky credentials from config file."""
    path = get_credentials_path()
    if not path.exists():
        return None

    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_session(session_string: str) -> None:
    """Save AT Protocol session string for reuse."""
    path = get_session_path()
    path.write_text(session_string)
    path.chmod(0o600)


def load_session() -> str | None:
    """Load saved AT Protocol session string."""
    path = get_session_path()
    if not path.exists():
        return None

    try:
        return path.read_text()
    except OSError:
        return None


def create_client(handle: str | None = None, password: str | None = None) -> "Client":
    """Create and authenticate AT Protocol client.

    Args:
        handle: Bluesky handle (e.g., 'user.bsky.social'). If not provided, loads from credentials.
        password: Account password. If not provided, loads from credentials.

    Returns:
        Authenticated AT Protocol client.

    Raises:
        ValueError: If credentials are not provided and not found in config.
        Exception: If authentication fails.
    """
    from atproto import Client

    client = Client()

    # Load credentials if not provided
    if not handle or not password:
        creds = load_credentials()
        if not creds:
            raise ValueError(
                "No credentials provided or found. "
                "Run 'lestash bluesky auth' first or provide handle and password."
            )
        handle = handle or creds["handle"]
        password = password or creds["password"]

    # Try to restore session first (faster than full login)
    session_string = load_session()
    if session_string:
        try:
            # Attempt to use saved session
            client.login(session_string=session_string)
            # Verify it's the right handle
            if client.me and (client.me.handle == handle or client.me.did == handle):
                return client
            # Wrong handle, fall through to full login
        except Exception:
            # Session expired or invalid, fall through to full login
            pass

    # Full login
    try:
        client.login(handle, password)

        # Save session for reuse
        session_string = client.export_session_string()
        save_session(session_string)

        return client
    except Exception as e:
        raise Exception(f"Authentication failed: {e}") from e


def get_author_posts(client: "Client", actor: str, limit: int = 100) -> list["models.AppBskyFeedDefs.FeedViewPost"]:
    """Fetch all posts from an author.

    Args:
        client: Authenticated AT Protocol client
        actor: Actor handle or DID
        limit: Number of posts to fetch per request (max 100)

    Returns:
        List of feed view posts
    """
    posts = []
    cursor = None

    while True:
        response = client.app.bsky.feed.get_author_feed(
            {
                "actor": actor,
                "limit": limit,
                "cursor": cursor,
            }
        )

        posts.extend(response.feed)

        # Check if there are more posts
        if not response.cursor:
            break

        cursor = response.cursor

    return posts
