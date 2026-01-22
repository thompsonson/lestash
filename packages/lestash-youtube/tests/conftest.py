"""Test fixtures for YouTube plugin tests."""

from typing import Any

import pytest


@pytest.fixture
def youtube_video_factory():
    """Factory to create mock YouTube video data.

    Returns a function that creates video data dictionaries matching
    the structure returned by the YouTube Data API v3.
    """

    def _create_video(
        video_id: str = "dQw4w9WgXcQ",
        title: str = "Test Video Title",
        description: str = "Test video description",
        channel_id: str = "UC_test_channel_123",
        channel_title: str = "Test Channel",
        published_at: str = "2025-01-15T10:30:00Z",
        duration: str = "PT4M35S",
        definition: str = "hd",
        view_count: str = "1000000",
        like_count: str = "50000",
        comment_count: str = "5000",
        tags: list[str] | None = None,
        category_id: str = "22",
        thumbnails: dict | None = None,
    ) -> dict[str, Any]:
        """Create mock video data.

        Args:
            video_id: YouTube video ID
            title: Video title
            description: Video description
            channel_id: Channel ID
            channel_title: Channel display name
            published_at: ISO 8601 publish timestamp
            duration: ISO 8601 duration (e.g., PT4M35S)
            definition: Video definition (hd/sd)
            view_count: Number of views
            like_count: Number of likes
            comment_count: Number of comments
            tags: Video tags
            category_id: YouTube category ID
            thumbnails: Thumbnail URLs dict
        """
        if thumbnails is None:
            thumbnails = {
                "default": {"url": f"https://i.ytimg.com/vi/{video_id}/default.jpg"},
                "medium": {"url": f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"},
                "high": {"url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"},
            }

        return {
            "id": video_id,
            "title": title,
            "description": description,
            "channel_id": channel_id,
            "channel_title": channel_title,
            "published_at": published_at,
            "duration": duration,
            "definition": definition,
            "view_count": view_count,
            "like_count": like_count,
            "comment_count": comment_count,
            "tags": tags or ["test", "video"],
            "category_id": category_id,
            "thumbnails": thumbnails,
        }

    return _create_video


@pytest.fixture
def youtube_subscription_factory():
    """Factory to create mock YouTube subscription data."""

    def _create_subscription(
        subscription_id: str = "sub_abc123",
        channel_id: str = "UC_subscribed_channel",
        title: str = "Subscribed Channel",
        description: str = "Channel description",
        published_at: str = "2024-06-15T08:00:00Z",
        thumbnails: dict | None = None,
    ) -> dict[str, Any]:
        """Create mock subscription data."""
        if thumbnails is None:
            thumbnails = {
                "default": {"url": "https://example.com/thumb_default.jpg"},
                "medium": {"url": "https://example.com/thumb_medium.jpg"},
                "high": {"url": "https://example.com/thumb_high.jpg"},
            }

        return {
            "id": subscription_id,
            "channel_id": channel_id,
            "title": title,
            "description": description,
            "published_at": published_at,
            "thumbnails": thumbnails,
        }

    return _create_subscription


@pytest.fixture
def youtube_history_video_factory():
    """Factory to create mock YouTube watch history video data."""

    def _create_history_video(
        video_id: str = "history_vid_123",
        title: str = "Watched Video",
        description: str = "Video I watched",
        channel_id: str = "UC_watched_channel",
        channel_title: str = "Watched Channel",
        published_at: str = "2025-01-10T14:00:00Z",
        watched_at: str = "2025-01-20T19:30:00Z",
        thumbnails: dict | None = None,
    ) -> dict[str, Any]:
        """Create mock history video data."""
        if thumbnails is None:
            thumbnails = {
                "default": {"url": f"https://i.ytimg.com/vi/{video_id}/default.jpg"},
            }

        return {
            "id": video_id,
            "title": title,
            "description": description,
            "channel_id": channel_id,
            "channel_title": channel_title,
            "published_at": published_at,
            "watched_at": watched_at,
            "thumbnails": thumbnails,
        }

    return _create_history_video
