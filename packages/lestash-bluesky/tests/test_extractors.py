"""Tests for Bluesky post extraction and transformation - Fixed version."""

from types import SimpleNamespace

from lestash.models.item import ItemCreate
from lestash_bluesky.source import extract_text_from_facets, post_to_item

from tests.conftest import MockLink, MockMention, MockTag


class TestCorePostExtraction:
    """Test basic post data extraction."""

    def test_extracts_post_text(self, bluesky_post_factory):
        """Should extract the post text from record.text."""
        post = bluesky_post_factory(text="This is my test post")

        item = post_to_item(post, "user.bsky.social")

        assert item.content == "This is my test post"

    def test_parses_created_at_timestamp(self, bluesky_post_factory):
        """Should parse ISO 8601 timestamp to datetime."""
        post = bluesky_post_factory(created_at="2025-01-22T15:30:45.123Z")

        item = post_to_item(post, "user.bsky.social")

        assert item.created_at is not None
        assert item.created_at.year == 2025
        assert item.created_at.month == 1
        assert item.created_at.day == 22
        assert item.created_at.hour == 15
        assert item.created_at.minute == 30
        assert item.created_at.second == 45

    def test_builds_post_url_from_uri(self, bluesky_post_factory):
        """Should construct bsky.app URL from handle and URI."""
        post = bluesky_post_factory(
            handle="alice.bsky.social",
            uri="at://did:plc:alice123/app.bsky.feed.post/abc123xyz",
        )

        item = post_to_item(post, "alice.bsky.social")

        assert item.url == "https://bsky.app/profile/alice.bsky.social/post/abc123xyz"

    def test_sets_source_type_and_id(self, bluesky_post_factory):
        """Should set source_type to 'bluesky' and source_id to URI."""
        post = bluesky_post_factory(uri="at://did:plc:test/app.bsky.feed.post/unique123")

        item = post_to_item(post, "user.bsky.social")

        assert item.source_type == "bluesky"
        assert item.source_id == "at://did:plc:test/app.bsky.feed.post/unique123"

    def test_uses_display_name_as_author(self, bluesky_post_factory):
        """Should use display_name as author when available."""
        post = bluesky_post_factory(
            handle="user.bsky.social",
            display_name="Alice Smith",
        )

        item = post_to_item(post, "user.bsky.social")

        assert item.author == "Alice Smith"

    def test_falls_back_to_handle_when_no_display_name(self, bluesky_post_factory):
        """Should fall back to handle when display_name is None."""
        post = bluesky_post_factory(
            handle="bob.bsky.social",
            display_name=None,
        )

        item = post_to_item(post, "user.bsky.social")

        assert item.author == "bob.bsky.social"


class TestAuthorDetection:
    """Test is_own_content detection."""

    def test_is_own_content_matches_by_handle(self, bluesky_post_factory):
        """Should set is_own_content=True when handle matches."""
        post = bluesky_post_factory(handle="alice.bsky.social")

        item = post_to_item(post, "alice.bsky.social")

        assert item.is_own_content is True

    def test_is_own_content_false_for_others(self, bluesky_post_factory):
        """Should set is_own_content=False for other authors."""
        post = bluesky_post_factory(handle="bob.bsky.social")

        item = post_to_item(post, "alice.bsky.social")

        assert item.is_own_content is False


class TestFacetsExtraction:
    """Test rich text facets (mentions, links, hashtags)."""

    def test_extracts_mentions_from_facets(self, bluesky_post_factory):
        """Should extract @mention DIDs from facets."""
        # Use mock classes directly
        mention1 = MockMention("did:plc:alice123")
        mention2 = MockMention("did:plc:bob456")

        facet1 = SimpleNamespace(features=[mention1])
        facet2 = SimpleNamespace(features=[mention2])

        post = bluesky_post_factory(
            text="Hello @alice and @bob!",
            facets=[facet1, facet2],
        )

        item = post_to_item(post, "user.bsky.social")

        assert "mentions" in item.metadata["facets"]
        assert "did:plc:alice123" in item.metadata["facets"]["mentions"]
        assert "did:plc:bob456" in item.metadata["facets"]["mentions"]

    def test_extracts_links_from_facets(self, bluesky_post_factory):
        """Should extract URLs from facets."""
        link = MockLink("https://example.com")

        facet = SimpleNamespace(features=[link])

        post = bluesky_post_factory(
            text="Check out https://example.com",
            facets=[facet],
        )

        item = post_to_item(post, "user.bsky.social")

        assert "links" in item.metadata["facets"]
        assert "https://example.com" in item.metadata["facets"]["links"]

    def test_extracts_hashtags_from_facets(self, bluesky_post_factory):
        """Should extract #hashtags from facets."""
        tag = MockTag("test")

        facet = SimpleNamespace(features=[tag])

        post = bluesky_post_factory(
            text="This is a #test post",
            facets=[facet],
        )

        item = post_to_item(post, "user.bsky.social")

        assert "hashtags" in item.metadata["facets"]
        assert "test" in item.metadata["facets"]["hashtags"]

    def test_handles_missing_facets(self, bluesky_post_factory):
        """Should handle posts without facets gracefully."""
        post = bluesky_post_factory(text="Plain text post", facets=None)

        item = post_to_item(post, "user.bsky.social")

        assert item.metadata["facets"]["mentions"] == []
        assert item.metadata["facets"]["links"] == []
        assert item.metadata["facets"]["hashtags"] == []


class TestEmbeds:
    """Test embedded content (images, links, quotes, videos)."""

    def test_embed_images_stores_alt_and_aspect_ratio(
        self, bluesky_post_factory, mock_embed_images
    ):
        """Should store image alt text and aspect ratio."""
        embed = mock_embed_images(
            images=[
                {
                    "alt": "A sunset",
                    "aspect_ratio": SimpleNamespace(width=1200, height=800),
                }
            ]
        )

        post = bluesky_post_factory(text="Beautiful sunset!", embed=embed)

        item = post_to_item(post, "user.bsky.social")

        assert item.metadata["embed_type"] == "MockEmbedImagesMain"
        assert "images" in item.metadata
        assert len(item.metadata["images"]) == 1
        assert item.metadata["images"][0]["alt"] == "A sunset"
        assert item.metadata["images"][0]["aspect_ratio"]["width"] == 1200
        assert item.metadata["images"][0]["aspect_ratio"]["height"] == 800

    def test_embed_external_stores_uri_title_description(
        self, bluesky_post_factory, mock_embed_external
    ):
        """Should store external link metadata."""
        embed = mock_embed_external(
            uri="https://blog.example.com/post",
            title="Great Blog Post",
            description="A really interesting article",
        )

        post = bluesky_post_factory(text="Check this out!", embed=embed)

        item = post_to_item(post, "user.bsky.social")

        assert item.metadata["embed_type"] == "MockEmbedExternalMain"
        assert item.metadata["external"]["uri"] == "https://blog.example.com/post"
        assert item.metadata["external"]["title"] == "Great Blog Post"
        assert item.metadata["external"]["description"] == "A really interesting article"

    def test_embed_quote_stores_uri(self, bluesky_post_factory, mock_embed_record):
        """Should store quoted post URI."""
        embed = mock_embed_record(uri="at://did:plc:other/app.bsky.feed.post/quoted123")

        post = bluesky_post_factory(text="Quoting this!", embed=embed)

        item = post_to_item(post, "user.bsky.social")

        assert item.metadata["embed_type"] == "MockEmbedRecordMain"
        assert item.metadata["quoted_post"] == "at://did:plc:other/app.bsky.feed.post/quoted123"

    def test_embed_video_stores_metadata(self, bluesky_post_factory, mock_embed_video):
        """Should store video metadata."""
        embed = mock_embed_video(video_cid="bafyvideo789", alt="My video")

        post = bluesky_post_factory(text="Watch this!", embed=embed)

        item = post_to_item(post, "user.bsky.social")

        # Video embeds are stored but not specially handled yet
        assert item.metadata["embed_type"] == "MockEmbedVideoMain"

    def test_no_embed_metadata_when_missing(self, bluesky_post_factory):
        """Should not include embed fields when no embed present."""
        post = bluesky_post_factory(text="Plain post", embed=None)

        item = post_to_item(post, "user.bsky.social")

        assert "embed_type" not in item.metadata
        assert "images" not in item.metadata
        assert "external" not in item.metadata
        assert "quoted_post" not in item.metadata


class TestReplies:
    """Test reply/thread handling."""

    def test_reply_stores_parent_and_root_uris(self, bluesky_post_factory):
        """Should store parent and root URIs for replies."""
        post = bluesky_post_factory(
            text="Great point!",
            reply={
                "parent": "at://did:plc:other/app.bsky.feed.post/parent123",
                "root": "at://did:plc:original/app.bsky.feed.post/root123",
            },
        )

        item = post_to_item(post, "user.bsky.social")

        assert "reply_to" in item.metadata
        assert (
            item.metadata["reply_to"]["parent"] == "at://did:plc:other/app.bsky.feed.post/parent123"
        )
        assert (
            item.metadata["reply_to"]["root"] == "at://did:plc:original/app.bsky.feed.post/root123"
        )

    def test_no_reply_metadata_for_standalone_post(self, bluesky_post_factory):
        """Should not include reply_to for standalone posts."""
        post = bluesky_post_factory(text="Standalone post", reply=None)

        item = post_to_item(post, "user.bsky.social")

        assert "reply_to" not in item.metadata


class TestMetadata:
    """Test metadata storage."""

    def test_stores_cid_and_engagement_metrics(self, bluesky_post_factory):
        """Should store CID and engagement counts."""
        post = bluesky_post_factory(
            cid="bafycid123",
            reply_count=5,
            repost_count=12,
            like_count=42,
        )

        item = post_to_item(post, "user.bsky.social")

        assert item.metadata["cid"] == "bafycid123"
        assert item.metadata["reply_count"] == 5
        assert item.metadata["repost_count"] == 12
        assert item.metadata["like_count"] == 42


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_handles_empty_post_text(self, bluesky_post_factory):
        """Should handle posts with empty text."""
        post = bluesky_post_factory(text="")

        item = post_to_item(post, "user.bsky.social")

        assert item.content == ""
        assert item.source_type == "bluesky"

    def test_handles_missing_created_at(self, bluesky_post_factory):
        """Should handle missing created_at gracefully."""
        post = bluesky_post_factory()
        # Remove created_at attribute
        delattr(post.post.record, "created_at")

        item = post_to_item(post, "user.bsky.social")

        assert item.created_at is None

    def test_handles_invalid_timestamp_format(self, bluesky_post_factory):
        """Should handle malformed timestamps gracefully."""
        post = bluesky_post_factory(created_at="invalid-timestamp")

        item = post_to_item(post, "user.bsky.social")

        # Should not raise, just set created_at to None
        assert item.created_at is None

    def test_handles_null_facets_gracefully(self, bluesky_post_factory):
        """Should handle null facets without errors."""
        post = bluesky_post_factory(text="Test", facets=None)

        item = post_to_item(post, "user.bsky.social")

        assert item.metadata["facets"]["mentions"] == []
        assert item.metadata["facets"]["links"] == []
        assert item.metadata["facets"]["hashtags"] == []

    def test_handles_minimal_post_structure(self, bluesky_post_factory):
        """Should handle posts with minimal required fields."""
        post = bluesky_post_factory(
            text="Minimal",
            facets=None,
            embed=None,
            reply=None,
            langs=None,
            reply_count=0,
            repost_count=0,
            like_count=0,
        )

        item = post_to_item(post, "user.bsky.social")

        assert isinstance(item, ItemCreate)
        assert item.content == "Minimal"
        assert item.source_type == "bluesky"


class TestExtractTextFromFacets:
    """Test the extract_text_from_facets helper function."""

    def test_extracts_multiple_facet_types(self):
        """Should extract mentions, links, and hashtags from mixed facets."""
        mention = MockMention("did:plc:user123")
        link = MockLink("https://example.com")
        tag = MockTag("python")

        facets = [
            SimpleNamespace(features=[mention, link]),
            SimpleNamespace(features=[tag]),
        ]

        result = extract_text_from_facets("text", facets)

        assert "did:plc:user123" in result["mentions"]
        assert "https://example.com" in result["links"]
        assert "python" in result["hashtags"]

    def test_returns_empty_arrays_for_none_facets(self):
        """Should return empty arrays when facets is None."""
        result = extract_text_from_facets("text", None)

        assert result["mentions"] == []
        assert result["links"] == []
        assert result["hashtags"] == []

    def test_handles_empty_facets_list(self):
        """Should handle empty facets list."""
        result = extract_text_from_facets("text", [])

        assert result["mentions"] == []
        assert result["links"] == []
        assert result["hashtags"] == []
