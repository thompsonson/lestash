"""Test fixtures for Micro.blog plugin tests."""

from typing import Any
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_httpx_client():
    """Create a mock httpx client."""
    return MagicMock()


@pytest.fixture
def microblog_post_factory():
    """Factory to create mock Micro.blog h-entry objects.

    Returns a function that creates mock post objects matching
    the Micropub h-entry structure.
    """

    def _create_post(
        content: str = "Test post content",
        url: str = "https://micro.blog/user/12345",
        uid: str = "https://micro.blog/user/12345",
        name: str | None = None,
        published: str = "2026-01-22T10:00:00Z",
        author_name: str = "Test User",
        author_url: str = "https://micro.blog/user",
        categories: list[str] | None = None,
        photos: list[str] | None = None,
        syndication: list[str] | None = None,
        in_reply_to: list[str] | None = None,
        bookmark_of: list[str] | None = None,
        like_of: list[str] | None = None,
        content_html: str | None = None,
    ) -> dict[str, Any]:
        """Create a mock h-entry post.

        Args:
            content: Post text content.
            url: Post URL.
            uid: Unique identifier.
            name: Title (for longer posts).
            published: ISO 8601 datetime string.
            author_name: Author display name.
            author_url: Author profile URL.
            categories: List of tags/categories.
            photos: List of photo URLs.
            syndication: List of syndication URLs.
            in_reply_to: List of URLs this is replying to.
            bookmark_of: List of bookmarked URLs.
            like_of: List of liked URLs.
            content_html: HTML version of content.
        """
        properties: dict[str, Any] = {
            "url": [url],
            "uid": [uid],
            "published": [published],
            "author": [{"name": author_name, "url": author_url}],
        }

        # Content can be dict with html/value or just string
        if content_html:
            properties["content"] = [{"html": content_html, "value": content}]
        else:
            properties["content"] = [content]

        if name:
            properties["name"] = [name]

        if categories:
            properties["category"] = categories

        if photos:
            properties["photo"] = photos

        if syndication:
            properties["syndication"] = syndication

        if in_reply_to:
            properties["in-reply-to"] = in_reply_to

        if bookmark_of:
            properties["bookmark-of"] = bookmark_of

        if like_of:
            properties["like-of"] = like_of

        return {
            "type": ["h-entry"],
            "properties": properties,
        }

    return _create_post


@pytest.fixture
def mock_micropub_config():
    """Create a mock Micropub config response."""
    return {
        "media-endpoint": "https://micro.blog/micropub/media",
        "destination": [
            {"uid": "https://user.micro.blog/", "name": "Main Blog"},
            {"uid": "https://photos.user.micro.blog/", "name": "Photos"},
        ],
    }


@pytest.fixture
def mock_micropub_posts(microblog_post_factory):
    """Create a list of mock posts."""
    return [
        microblog_post_factory(
            content="First post",
            url="https://micro.blog/user/1",
            uid="https://micro.blog/user/1",
        ),
        microblog_post_factory(
            content="Second post with photo",
            url="https://micro.blog/user/2",
            uid="https://micro.blog/user/2",
            photos=["https://micro.blog/photos/abc.jpg"],
        ),
        microblog_post_factory(
            content="Third post with tags",
            url="https://micro.blog/user/3",
            uid="https://micro.blog/user/3",
            categories=["tech", "programming"],
        ),
    ]
