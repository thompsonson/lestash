"""Test fixtures for Bluesky plugin tests."""

import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


# Create mock atproto module for isinstance checks
_mock_atproto = ModuleType("atproto")
_mock_models = ModuleType("atproto.models")
_mock_richtext_facet = ModuleType("atproto.models.AppBskyRichtextFacet")
_mock_embed_images = ModuleType("atproto.models.AppBskyEmbedImages")
_mock_embed_external = ModuleType("atproto.models.AppBskyEmbedExternal")
_mock_embed_record = ModuleType("atproto.models.AppBskyEmbedRecord")
_mock_embed_video = ModuleType("atproto.models.AppBskyEmbedVideo")


# Mock classes that match atproto SDK structure for isinstance checks


class MockMention:
    """Mock AppBskyRichtextFacet.Mention."""

    def __init__(self, did: str):
        self.did = did


class MockLink:
    """Mock AppBskyRichtextFacet.Link."""

    def __init__(self, uri: str):
        self.uri = uri


class MockTag:
    """Mock AppBskyRichtextFacet.Tag."""

    def __init__(self, tag: str):
        self.tag = tag


class MockEmbedImagesMain:
    """Mock AppBskyEmbedImages.Main."""

    def __init__(self, images: list):
        self.images = images


class MockEmbedExternalMain:
    """Mock AppBskyEmbedExternal.Main."""

    def __init__(self, external):
        self.external = external


class MockEmbedRecordMain:
    """Mock AppBskyEmbedRecord.Main."""

    def __init__(self, record):
        self.record = record


class MockEmbedVideoMain:
    """Mock AppBskyEmbedVideo.Main."""

    def __init__(self, video):
        self.video = video


@pytest.fixture
def bluesky_post_factory():
    """Factory to create mock Bluesky FeedViewPost objects.

    Returns a function that creates mock post objects with the same structure
    as atproto.models.AppBskyFeedDefs.FeedViewPost without requiring the SDK.
    """

    def _create_post(
        text: str = "Test post",
        handle: str = "user.bsky.social",
        did: str = "did:plc:test123",
        display_name: str | None = "Test User",
        uri: str = "at://did:plc:test123/app.bsky.feed.post/abc123",
        cid: str = "bafytest123",
        created_at: str = "2025-01-22T10:00:00.000Z",
        facets: list | None = None,
        embed: Any | None = None,
        reply: dict | None = None,
        langs: list[str] | None = None,
        reply_count: int = 0,
        repost_count: int = 0,
        like_count: int = 0,
    ):
        """Create a mock FeedViewPost.

        Args:
            text: Post text content
            handle: Author handle
            did: Author DID
            display_name: Author display name (None to test fallback)
            uri: Post AT URI
            cid: Content identifier
            created_at: ISO 8601 timestamp
            facets: Rich text facets (mentions, links, hashtags)
            embed: Embedded content (images, external, record)
            reply: Reply parent/root info
            langs: Language tags
            reply_count: Number of replies
            repost_count: Number of reposts
            like_count: Number of likes
        """
        # Mock author
        author = SimpleNamespace(
            handle=handle,
            did=did,
            display_name=display_name,
        )

        # Mock record (post data)
        record = SimpleNamespace(
            text=text,
            created_at=created_at,
        )

        # Add facets if provided
        if facets is not None:
            record.facets = facets

        # Add embed if provided
        if embed is not None:
            record.embed = embed

        # Add reply if provided
        if reply is not None:
            record.reply = SimpleNamespace(
                parent=SimpleNamespace(uri=reply["parent"]),
                root=SimpleNamespace(uri=reply["root"]),
            )

        # Add langs if provided
        if langs is not None:
            record.langs = langs

        # Mock post wrapper
        post_data = SimpleNamespace(
            uri=uri,
            cid=cid,
            author=author,
            record=record,
            reply_count=reply_count,
            repost_count=repost_count,
            like_count=like_count,
        )

        # Mock FeedViewPost
        return SimpleNamespace(
            post=post_data,
        )

    return _create_post


@pytest.fixture
def mock_facet_mention():
    """Create a mock mention facet."""
    return MockMention


@pytest.fixture
def mock_facet_link():
    """Create a mock link facet."""
    return MockLink


@pytest.fixture
def mock_facet_tag():
    """Create a mock hashtag facet."""
    return MockTag


@pytest.fixture
def mock_embed_images():
    """Create a mock images embed."""

    def _create(images: list[dict] | None = None):
        if images is None:
            images = [
                {"alt": "Test image", "aspect_ratio": SimpleNamespace(width=800, height=600)},
            ]

        image_objs = [
            SimpleNamespace(
                alt=img["alt"],
                aspect_ratio=img.get("aspect_ratio"),
            )
            for img in images
        ]

        return MockEmbedImagesMain(image_objs)

    return _create


@pytest.fixture
def mock_embed_external():
    """Create a mock external link embed."""

    def _create(
        uri: str = "https://example.com",
        title: str = "Example",
        description: str = "An example link",
    ):
        external = SimpleNamespace(
            uri=uri,
            title=title,
            description=description,
        )
        return MockEmbedExternalMain(external)

    return _create


@pytest.fixture
def mock_embed_record():
    """Create a mock record (quote) embed."""

    def _create(uri: str = "at://did:plc:other/app.bsky.feed.post/xyz789"):
        record = SimpleNamespace(uri=uri)
        return MockEmbedRecordMain(record)

    return _create


@pytest.fixture
def mock_embed_video():
    """Create a mock video embed."""

    def _create(
        video_cid: str = "bafyvideo123",
        alt: str = "Test video",
    ):
        video = SimpleNamespace(
            ref=SimpleNamespace(link=video_cid),
            alt=alt,
        )
        return MockEmbedVideoMain(video)

    return _create


# Set up mock atproto module structure
_mock_richtext_facet.Mention = MockMention
_mock_richtext_facet.Link = MockLink
_mock_richtext_facet.Tag = MockTag
_mock_models.AppBskyRichtextFacet = _mock_richtext_facet

_mock_embed_images.Main = MockEmbedImagesMain
_mock_models.AppBskyEmbedImages = _mock_embed_images

_mock_embed_external.Main = MockEmbedExternalMain
_mock_models.AppBskyEmbedExternal = _mock_embed_external

_mock_embed_record.Main = MockEmbedRecordMain
_mock_models.AppBskyEmbedRecord = _mock_embed_record

_mock_embed_video.Main = MockEmbedVideoMain
_mock_models.AppBskyEmbedVideo = _mock_embed_video

_mock_atproto.models = _mock_models


@pytest.fixture(autouse=True)
def mock_atproto_module(monkeypatch):
    """Automatically mock the atproto module for all tests."""
    # Inject our mock module so isinstance checks work
    monkeypatch.setitem(sys.modules, "atproto", _mock_atproto)
    monkeypatch.setitem(sys.modules, "atproto.models", _mock_models)
    monkeypatch.setitem(sys.modules, "atproto.models.AppBskyRichtextFacet", _mock_richtext_facet)
    monkeypatch.setitem(sys.modules, "atproto.models.AppBskyEmbedImages", _mock_embed_images)
    monkeypatch.setitem(sys.modules, "atproto.models.AppBskyEmbedExternal", _mock_embed_external)
    monkeypatch.setitem(sys.modules, "atproto.models.AppBskyEmbedRecord", _mock_embed_record)
    monkeypatch.setitem(sys.modules, "atproto.models.AppBskyEmbedVideo", _mock_embed_video)
