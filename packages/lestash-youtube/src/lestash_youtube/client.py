"""YouTube Data API v3 client with OAuth 2.0 authentication.

Uses the shared Google OAuth module (lestash.core.google_auth) for authentication,
which supports headless/SSH environments and web UI auth.
"""

from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from lestash.core.google_auth import (
    get_client_secrets_path,
    get_credentials,
    run_auth_flow,
)

# YouTube API scopes
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
]


def check_client_secrets() -> bool:
    """Check if Google client secrets file exists."""
    return get_client_secrets_path().exists()


def run_oauth_flow() -> Credentials:
    """Run OAuth 2.0 flow for YouTube access.

    Delegates to the shared Google auth module which supports
    desktop, headless/SSH, and web UI flows.
    """
    return run_auth_flow(scopes=YOUTUBE_SCOPES)


def load_credentials() -> Credentials | None:
    """Load stored credentials (for status checks)."""
    from lestash.core.google_auth import load_credentials as _load

    return _load()


def get_youtube_credentials() -> Credentials:
    """Get valid OAuth credentials with YouTube scope.

    Raises:
        ValueError: If no credentials available.
    """
    return get_credentials(scopes=YOUTUBE_SCOPES)


def create_youtube_client():
    """Create authenticated YouTube API client.

    Returns:
        YouTube API service object.
    """
    credentials = get_youtube_credentials()
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


def get_transcript(video_id: str, languages: list[str] | None = None) -> dict[str, Any] | None:
    """Fetch transcript/captions for a YouTube video.

    Uses youtube-transcript-api (no Google auth needed for public videos).

    Args:
        video_id: YouTube video ID.
        languages: Preferred languages (default: ["en"]).

    Returns:
        Dict with full_text and segments, or None if unavailable.
    """
    from youtube_transcript_api import YouTubeTranscriptApi

    if languages is None:
        languages = ["en"]

    try:
        ytt_api = YouTubeTranscriptApi()
        transcript = ytt_api.fetch(video_id, languages=languages)
        segments = [
            {
                "text": entry.text,
                "start": entry.start,
                "duration": entry.duration,
            }
            for entry in transcript
        ]
        full_text = " ".join(str(s["text"]) for s in segments)
        return {"full_text": full_text, "segments": segments, "language": languages[0]}
    except Exception:
        return None


def get_comments(youtube: Any, video_id: str, max_results: int = 100) -> list[dict[str, Any]]:
    """Fetch top-level comments for a video.

    Args:
        youtube: Authenticated YouTube API client.
        video_id: YouTube video ID.
        max_results: Maximum number of comments to fetch.

    Returns:
        List of comment dicts with author, text, published_at, like_count.
    """
    comments: list[dict[str, Any]] = []
    try:
        response = (
            youtube.commentThreads()
            .list(
                part="snippet",
                videoId=video_id,
                maxResults=min(max_results, 100),
                order="relevance",
                textFormat="plainText",
            )
            .execute()
        )

        for item in response.get("items", []):
            snippet = item["snippet"]["topLevelComment"]["snippet"]
            comments.append(
                {
                    "id": item["snippet"]["topLevelComment"]["id"],
                    "author": snippet.get("authorDisplayName", ""),
                    "author_channel_id": snippet.get("authorChannelId", {}).get("value"),
                    "text": snippet.get("textDisplay", ""),
                    "published_at": snippet.get("publishedAt"),
                    "like_count": snippet.get("likeCount", 0),
                    "reply_count": item["snippet"].get("totalReplyCount", 0),
                }
            )
    except Exception:
        pass  # Comments may be disabled

    return comments
