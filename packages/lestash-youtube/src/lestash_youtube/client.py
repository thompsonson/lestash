"""YouTube Data API v3 client with OAuth 2.0 authentication."""

import json
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# YouTube API scopes
# - youtube.readonly: View account info (liked videos, subscriptions, etc.)
# - youtube: Full access (needed for some history operations)
SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
]


def get_config_dir() -> Path:
    """Get lestash config directory."""
    config_dir = Path.home() / ".config" / "lestash"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_client_secrets_path() -> Path:
    """Get path to YouTube OAuth client secrets file."""
    return get_config_dir() / "youtube_client_secrets.json"


def get_credentials_path() -> Path:
    """Get path to YouTube OAuth tokens file."""
    return get_config_dir() / "youtube_credentials.json"


def save_credentials(credentials: Credentials) -> None:
    """Save OAuth credentials to config file."""
    creds_data = {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": list(credentials.scopes) if credentials.scopes else SCOPES,
    }

    path = get_credentials_path()
    path.write_text(json.dumps(creds_data, indent=2))
    path.chmod(0o600)  # Make file readable only by owner


def load_credentials() -> Credentials | None:
    """Load OAuth credentials from config file."""
    path = get_credentials_path()
    if not path.exists():
        return None

    try:
        creds_data = json.loads(path.read_text())
        return Credentials(
            token=creds_data.get("token"),
            refresh_token=creds_data.get("refresh_token"),
            token_uri=creds_data.get("token_uri"),
            client_id=creds_data.get("client_id"),
            client_secret=creds_data.get("client_secret"),
            scopes=creds_data.get("scopes", SCOPES),
        )
    except (json.JSONDecodeError, OSError, KeyError):
        return None


def check_client_secrets() -> bool:
    """Check if client secrets file exists."""
    return get_client_secrets_path().exists()


def run_oauth_flow() -> Credentials:
    """Run OAuth 2.0 flow to get user credentials.

    Requires client_secrets.json to be present in config directory.

    Returns:
        Authenticated credentials.

    Raises:
        FileNotFoundError: If client secrets file is not found.
    """
    secrets_path = get_client_secrets_path()
    if not secrets_path.exists():
        raise FileNotFoundError(
            f"Client secrets file not found at {secrets_path}.\n"
            "Download it from Google Cloud Console:\n"
            "1. Go to https://console.cloud.google.com/apis/credentials\n"
            "2. Create OAuth 2.0 Client ID (Desktop application)\n"
            "3. Download the JSON and save it as:\n"
            f"   {secrets_path}"
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)

    # Run local server for OAuth callback
    credentials = flow.run_local_server(port=0)

    # Save credentials for future use
    save_credentials(credentials)

    return credentials


def get_credentials() -> Credentials:
    """Get valid OAuth credentials, refreshing if necessary.

    Returns:
        Valid credentials.

    Raises:
        ValueError: If no credentials available and can't authenticate.
    """
    credentials = load_credentials()

    if credentials and credentials.valid:
        return credentials

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            save_credentials(credentials)
            return credentials
        except Exception:
            # Refresh failed, need to re-authenticate
            pass

    raise ValueError("No valid credentials. Run 'lestash youtube auth' to authenticate.")


def create_youtube_client():
    """Create authenticated YouTube API client.

    Returns:
        YouTube API service object.
    """
    credentials = get_credentials()
    return build("youtube", "v3", credentials=credentials)


def get_liked_videos(youtube, max_results: int = 50) -> list[dict[str, Any]]:
    """Fetch user's liked videos with the timestamp of when they were liked.

    Uses the likes playlist to get the "liked at" timestamp, then fetches
    additional video details (duration, stats) for each video.

    Args:
        youtube: Authenticated YouTube API client.
        max_results: Maximum number of videos to fetch per page.

    Returns:
        List of video data dictionaries including 'liked_at' timestamp.
    """
    videos: list[dict[str, Any]] = []

    # First, get the user's "likes" playlist ID
    channels_response = (
        youtube.channels()
        .list(
            part="contentDetails",
            mine=True,
        )
        .execute()
    )

    if not channels_response.get("items"):
        return videos

    likes_playlist_id = (
        channels_response["items"][0]
        .get("contentDetails", {})
        .get("relatedPlaylists", {})
        .get("likes")
    )

    if not likes_playlist_id:
        return videos

    # Fetch items from the likes playlist
    # The snippet.publishedAt here is when the video was added to the playlist (liked)
    page_token = None
    playlist_items = []

    while True:
        request = youtube.playlistItems().list(
            part="snippet,contentDetails",
            playlistId=likes_playlist_id,
            maxResults=min(max_results, 50),
            pageToken=page_token,
        )
        response = request.execute()

        for item in response.get("items", []):
            snippet = item.get("snippet", {})
            video_id = snippet.get("resourceId", {}).get("videoId")
            if video_id:
                playlist_items.append(
                    {
                        "video_id": video_id,
                        # This is when the video was LIKED (added to the likes playlist)
                        "liked_at": snippet.get("publishedAt"),
                        # Basic info from playlist (video publish date and channel)
                        "title": snippet.get("title"),
                        "description": snippet.get("description"),
                        "channel_id": snippet.get("videoOwnerChannelId"),
                        "channel_title": snippet.get("videoOwnerChannelTitle"),
                        "thumbnails": snippet.get("thumbnails", {}),
                        # Video publish date from contentDetails
                        "published_at": item.get("contentDetails", {}).get("videoPublishedAt"),
                    }
                )

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    # Fetch additional video details (duration, stats) in batches
    video_ids = [item["video_id"] for item in playlist_items]
    video_details = {}

    # YouTube API allows up to 50 video IDs per request
    for i in range(0, len(video_ids), 50):
        batch_ids = video_ids[i : i + 50]
        details_response = (
            youtube.videos()
            .list(
                part="contentDetails,statistics,snippet",
                id=",".join(batch_ids),
            )
            .execute()
        )

        for item in details_response.get("items", []):
            video_details[item["id"]] = {
                "duration": item.get("contentDetails", {}).get("duration"),
                "definition": item.get("contentDetails", {}).get("definition"),
                "view_count": item.get("statistics", {}).get("viewCount"),
                "like_count": item.get("statistics", {}).get("likeCount"),
                "comment_count": item.get("statistics", {}).get("commentCount"),
                "tags": item.get("snippet", {}).get("tags", []),
                "category_id": item.get("snippet", {}).get("categoryId"),
            }

    # Combine playlist items with video details
    for item in playlist_items:
        video_id = item["video_id"]
        details = video_details.get(video_id, {})

        video = {
            "id": video_id,
            "title": item["title"],
            "description": item["description"],
            "channel_id": item["channel_id"],
            "channel_title": item["channel_title"],
            "published_at": item["published_at"],
            "liked_at": item["liked_at"],  # When the user liked it
            "thumbnails": item["thumbnails"],
            "tags": details.get("tags", []),
            "category_id": details.get("category_id"),
            "duration": details.get("duration"),
            "definition": details.get("definition"),
            "view_count": details.get("view_count"),
            "like_count": details.get("like_count"),
            "comment_count": details.get("comment_count"),
        }
        videos.append(video)

    return videos


def get_watch_history(youtube, max_results: int = 50) -> list[dict[str, Any]]:
    """Attempt to fetch user's watch history.

    Note: This may return empty results due to YouTube API restrictions.
    Watch history playlist access has been deprecated for most use cases.

    Args:
        youtube: Authenticated YouTube API client.
        max_results: Maximum number of videos to fetch per page.

    Returns:
        List of video data dictionaries (may be empty).
    """
    videos: list[dict[str, Any]] = []

    try:
        # First, get the channel's watch history playlist ID
        channels_response = (
            youtube.channels()
            .list(
                part="contentDetails",
                mine=True,
            )
            .execute()
        )

        if not channels_response.get("items"):
            return videos

        # Get watch history playlist ID
        watch_history_id = (
            channels_response["items"][0]
            .get("contentDetails", {})
            .get("relatedPlaylists", {})
            .get("watchHistory")
        )

        if not watch_history_id:
            return videos

        # Try to fetch watch history playlist items
        page_token = None

        while True:
            request = youtube.playlistItems().list(
                part="snippet,contentDetails",
                playlistId=watch_history_id,
                maxResults=min(max_results, 50),
                pageToken=page_token,
            )

            try:
                response = request.execute()
            except Exception:
                # Watch history access is often restricted
                break

            for item in response.get("items", []):
                snippet = item.get("snippet", {})
                video = {
                    "id": snippet.get("resourceId", {}).get("videoId"),
                    "title": snippet.get("title"),
                    "description": snippet.get("description"),
                    "channel_id": snippet.get("videoOwnerChannelId"),
                    "channel_title": snippet.get("videoOwnerChannelTitle"),
                    "published_at": snippet.get("publishedAt"),
                    "watched_at": item.get("contentDetails", {}).get("videoPublishedAt"),
                    "thumbnails": snippet.get("thumbnails", {}),
                }
                if video["id"]:
                    videos.append(video)

            page_token = response.get("nextPageToken")
            if not page_token:
                break

    except Exception:
        # Watch history access is restricted - return empty list
        pass

    return videos


def get_subscriptions(youtube, max_results: int = 50) -> list[dict[str, Any]]:
    """Fetch user's channel subscriptions.

    Args:
        youtube: Authenticated YouTube API client.
        max_results: Maximum number of subscriptions to fetch per page.

    Returns:
        List of subscription data dictionaries.
    """
    subscriptions = []
    page_token = None

    while True:
        request = youtube.subscriptions().list(
            part="snippet",
            mine=True,
            maxResults=min(max_results, 50),
            pageToken=page_token,
        )
        response = request.execute()

        for item in response.get("items", []):
            snippet = item.get("snippet", {})
            sub = {
                "id": item["id"],
                "channel_id": snippet.get("resourceId", {}).get("channelId"),
                "title": snippet.get("title"),
                "description": snippet.get("description"),
                "published_at": snippet.get("publishedAt"),
                "thumbnails": snippet.get("thumbnails", {}),
            }
            subscriptions.append(sub)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return subscriptions


def get_channel_info(youtube) -> dict[str, Any] | None:
    """Get authenticated user's channel information.

    Args:
        youtube: Authenticated YouTube API client.

    Returns:
        Channel information dictionary or None if not found.
    """
    response = (
        youtube.channels()
        .list(
            part="snippet,statistics,contentDetails",
            mine=True,
        )
        .execute()
    )

    if not response.get("items"):
        return None

    channel = response["items"][0]
    return {
        "id": channel["id"],
        "title": channel["snippet"]["title"],
        "description": channel["snippet"].get("description", ""),
        "custom_url": channel["snippet"].get("customUrl"),
        "published_at": channel["snippet"]["publishedAt"],
        "subscriber_count": channel.get("statistics", {}).get("subscriberCount"),
        "video_count": channel.get("statistics", {}).get("videoCount"),
        "view_count": channel.get("statistics", {}).get("viewCount"),
    }
