"""Tests to ensure all atproto objects are properly serialized for JSON storage."""

import json
from types import SimpleNamespace

import pytest

from lestash_bluesky.source import post_to_item
from tests.conftest import MockLink, MockMention, MockTag


class TestJSONSerialization:
    """Verify that all post data can be serialized to JSON."""

    def test_post_with_images_is_json_serializable(
        self, bluesky_post_factory, mock_embed_images
    ):
        """Should serialize posts with image embeds to JSON."""
        # Use SimpleNamespace for aspect_ratio as expected by mock factory
        embed = mock_embed_images(
            images=[
                {
                    "alt": "Image 1",
                    "aspect_ratio": SimpleNamespace(width=1200, height=800),
                },
                {
                    "alt": "Image 2",
                    "aspect_ratio": SimpleNamespace(width=1600, height=900),
                },
            ]
        )

        post = bluesky_post_factory(text="Check out these photos!", embed=embed)
        item = post_to_item(post, "user.bsky.social")

        # Should not raise JSONDecodeError
        metadata_json = json.dumps(item.metadata)
        assert metadata_json is not None

        # Verify round-trip
        restored = json.loads(metadata_json)
        assert restored["images"][0]["aspect_ratio"]["width"] == 1200
        assert restored["images"][1]["aspect_ratio"]["height"] == 900

    def test_post_with_external_link_is_json_serializable(
        self, bluesky_post_factory, mock_embed_external
    ):
        """Should serialize posts with external embeds to JSON."""
        embed = mock_embed_external(
            uri="https://example.com/article",
            title="Great Article",
            description="An interesting read",
        )

        post = bluesky_post_factory(text="Check this out!", embed=embed)
        item = post_to_item(post, "user.bsky.social")

        # Should not raise JSONDecodeError
        metadata_json = json.dumps(item.metadata)
        assert metadata_json is not None

        # Verify round-trip
        restored = json.loads(metadata_json)
        assert restored["external"]["uri"] == "https://example.com/article"

    def test_post_with_quote_is_json_serializable(
        self, bluesky_post_factory, mock_embed_record
    ):
        """Should serialize posts with record embeds (quotes) to JSON."""
        embed = mock_embed_record(uri="at://did:plc:other/app.bsky.feed.post/quoted123")

        post = bluesky_post_factory(text="Quoting this!", embed=embed)
        item = post_to_item(post, "user.bsky.social")

        # Should not raise JSONDecodeError
        metadata_json = json.dumps(item.metadata)
        assert metadata_json is not None

        # Verify round-trip
        restored = json.loads(metadata_json)
        assert restored["quoted_post"] == "at://did:plc:other/app.bsky.feed.post/quoted123"

    def test_post_with_reply_is_json_serializable(self, bluesky_post_factory):
        """Should serialize posts with reply metadata to JSON."""
        post = bluesky_post_factory(
            text="Great point!",
            reply={
                "parent": "at://did:plc:other/app.bsky.feed.post/parent123",
                "root": "at://did:plc:original/app.bsky.feed.post/root123",
            },
        )

        item = post_to_item(post, "user.bsky.social")

        # Should not raise JSONDecodeError
        metadata_json = json.dumps(item.metadata)
        assert metadata_json is not None

        # Verify round-trip
        restored = json.loads(metadata_json)
        assert restored["reply_to"]["parent"] == "at://did:plc:other/app.bsky.feed.post/parent123"

    def test_post_with_facets_is_json_serializable(self, bluesky_post_factory):
        """Should serialize posts with facets (mentions, links, hashtags) to JSON."""
        # Create facets using mock classes
        mention = MockMention("did:plc:alice123")
        link = MockLink("https://example.com")
        tag = MockTag("cool")

        facet1 = SimpleNamespace(features=[mention])
        facet2 = SimpleNamespace(features=[link])
        facet3 = SimpleNamespace(features=[tag])

        post = bluesky_post_factory(
            text="Hey @alice check out https://example.com #cool",
            facets=[facet1, facet2, facet3],
        )

        item = post_to_item(post, "user.bsky.social")

        # Should not raise JSONDecodeError
        metadata_json = json.dumps(item.metadata)
        assert metadata_json is not None

        # Verify round-trip
        restored = json.loads(metadata_json)
        assert "did:plc:alice123" in restored["facets"]["mentions"]
        assert "https://example.com" in restored["facets"]["links"]
        assert "cool" in restored["facets"]["hashtags"]

    def test_minimal_post_is_json_serializable(self, bluesky_post_factory):
        """Should serialize minimal posts with no embeds or facets to JSON."""
        post = bluesky_post_factory(text="Simple post")

        item = post_to_item(post, "user.bsky.social")

        # Should not raise JSONDecodeError
        metadata_json = json.dumps(item.metadata)
        assert metadata_json is not None

        # Verify round-trip - use actual default values from factory
        restored = json.loads(metadata_json)
        assert restored["cid"] == "bafytest123"
        assert restored["uri"] == "at://did:plc:test123/app.bsky.feed.post/abc123"

    def test_full_post_with_all_features_is_json_serializable(
        self, bluesky_post_factory, mock_embed_images
    ):
        """Should serialize complex posts with multiple features to JSON."""
        # Create facets
        mention = MockMention("did:plc:alice123")
        link = MockLink("https://example.com")
        tag = MockTag("cool")

        facet1 = SimpleNamespace(features=[mention])
        facet2 = SimpleNamespace(features=[link])
        facet3 = SimpleNamespace(features=[tag])

        # Create embed
        embed = mock_embed_images(
            images=[
                {
                    "alt": "Test image",
                    "aspect_ratio": SimpleNamespace(width=1200, height=800),
                }
            ]
        )

        post = bluesky_post_factory(
            text="Hey @alice check out this image! https://example.com #cool",
            facets=[facet1, facet2, facet3],
            embed=embed,
            reply={
                "parent": "at://did:plc:other/app.bsky.feed.post/parent123",
                "root": "at://did:plc:original/app.bsky.feed.post/root123",
            },
            langs=["en"],
        )

        item = post_to_item(post, "user.bsky.social")

        # Should not raise JSONDecodeError - this is the critical test
        metadata_json = json.dumps(item.metadata)
        assert metadata_json is not None

        # Verify round-trip preserves all data
        restored = json.loads(metadata_json)
        assert "cid" in restored
        assert "uri" in restored
        assert "images" in restored
        assert "reply_to" in restored
        assert "facets" in restored
        assert "langs" in restored
        assert restored["langs"] == ["en"]
