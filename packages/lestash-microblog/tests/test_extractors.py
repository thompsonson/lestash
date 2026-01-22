"""Tests for h-entry to ItemCreate transformation."""

from datetime import UTC, datetime

from lestash_microblog.source import (
    extract_content,
    extract_property,
    extract_property_list,
    parse_datetime,
    post_to_item,
)


class TestExtractProperty:
    """Test extract_property helper."""

    def test_extracts_first_value(self):
        """Should extract first value from property list."""
        entry = {"properties": {"url": ["https://example.com/1", "https://example.com/2"]}}

        result = extract_property(entry, "url")

        assert result == "https://example.com/1"

    def test_returns_none_for_missing_property(self):
        """Should return None when property doesn't exist."""
        entry = {"properties": {}}

        result = extract_property(entry, "missing")

        assert result is None

    def test_returns_none_for_empty_list(self):
        """Should return None when property list is empty."""
        entry = {"properties": {"empty": []}}

        result = extract_property(entry, "empty")

        assert result is None

    def test_returns_none_for_missing_properties(self):
        """Should return None when properties dict is missing."""
        entry = {}

        result = extract_property(entry, "anything")

        assert result is None


class TestExtractPropertyList:
    """Test extract_property_list helper."""

    def test_extracts_full_list(self):
        """Should return full property list."""
        entry = {"properties": {"category": ["tech", "programming", "python"]}}

        result = extract_property_list(entry, "category")

        assert result == ["tech", "programming", "python"]

    def test_returns_empty_list_for_missing(self):
        """Should return empty list when property doesn't exist."""
        entry = {"properties": {}}

        result = extract_property_list(entry, "missing")

        assert result == []


class TestExtractContent:
    """Test extract_content helper."""

    def test_extracts_string_content(self, microblog_post_factory):
        """Should extract plain string content."""
        entry = microblog_post_factory(content="Hello world")

        result = extract_content(entry)

        assert result == "Hello world"

    def test_extracts_content_from_dict(self, microblog_post_factory):
        """Should extract value from content dict."""
        entry = microblog_post_factory(
            content="Plain text",
            content_html="<p>Plain text</p>",
        )

        result = extract_content(entry)

        assert result == "Plain text"

    def test_falls_back_to_html_when_no_value(self):
        """Should use HTML when value is missing."""
        entry = {
            "properties": {
                "content": [{"html": "<p>HTML content</p>"}],
            }
        }

        result = extract_content(entry)

        assert result == "<p>HTML content</p>"

    def test_returns_empty_for_missing_content(self):
        """Should return empty string when no content."""
        entry = {"properties": {}}

        result = extract_content(entry)

        assert result == ""


class TestParseDatetime:
    """Test parse_datetime helper."""

    def test_parses_iso_format_with_z(self):
        """Should parse ISO format with Z suffix."""
        result = parse_datetime("2026-01-22T10:00:00Z")

        assert result == datetime(2026, 1, 22, 10, 0, 0, tzinfo=UTC)

    def test_parses_iso_format_with_offset(self):
        """Should parse ISO format with timezone offset."""
        result = parse_datetime("2026-01-22T10:00:00+00:00")

        assert result is not None
        assert result.hour == 10

    def test_returns_none_for_invalid(self):
        """Should return None for invalid datetime."""
        result = parse_datetime("not a datetime")

        assert result is None

    def test_returns_none_for_none(self):
        """Should return None for None input."""
        result = parse_datetime(None)

        assert result is None


class TestPostToItem:
    """Test post_to_item transformation."""

    def test_basic_post_conversion(self, microblog_post_factory):
        """Should convert basic post to ItemCreate."""
        entry = microblog_post_factory(
            content="Test post content",
            url="https://micro.blog/user/12345",
        )

        item = post_to_item(entry)

        assert item.source_type == "microblog"
        assert item.content == "Test post content"
        assert item.url == "https://micro.blog/user/12345"

    def test_extracts_source_id_from_url(self, microblog_post_factory):
        """Should use URL as source_id."""
        entry = microblog_post_factory(url="https://micro.blog/user/abc123")

        item = post_to_item(entry)

        assert item.source_id == "https://micro.blog/user/abc123"

    def test_extracts_title_from_name(self, microblog_post_factory):
        """Should use name property as title."""
        entry = microblog_post_factory(
            content="Post content",
            name="My Long Post Title",
        )

        item = post_to_item(entry)

        assert item.title == "My Long Post Title"

    def test_extracts_author_name(self, microblog_post_factory):
        """Should extract author name."""
        entry = microblog_post_factory(author_name="John Doe")

        item = post_to_item(entry)

        assert item.author == "John Doe"

    def test_parses_published_datetime(self, microblog_post_factory):
        """Should parse published datetime."""
        entry = microblog_post_factory(published="2026-01-22T15:30:00Z")

        item = post_to_item(entry)

        assert item.created_at is not None
        assert item.created_at.year == 2026
        assert item.created_at.month == 1
        assert item.created_at.day == 22

    def test_sets_is_own_content(self, microblog_post_factory):
        """Should set is_own_content flag."""
        entry = microblog_post_factory()

        item = post_to_item(entry, is_own=True)

        assert item.is_own_content is True

    def test_includes_categories_in_metadata(self, microblog_post_factory):
        """Should include categories in metadata."""
        entry = microblog_post_factory(categories=["tech", "python"])

        item = post_to_item(entry)

        assert item.metadata is not None
        assert item.metadata["categories"] == ["tech", "python"]

    def test_includes_photos_in_metadata(self, microblog_post_factory):
        """Should include photos in metadata."""
        entry = microblog_post_factory(
            photos=["https://example.com/1.jpg", "https://example.com/2.jpg"]
        )

        item = post_to_item(entry)

        assert item.metadata is not None
        assert len(item.metadata["photos"]) == 2

    def test_includes_syndication_in_metadata(self, microblog_post_factory):
        """Should include syndication URLs in metadata."""
        entry = microblog_post_factory(
            syndication=["https://twitter.com/user/123", "https://mastodon.social/@user/456"]
        )

        item = post_to_item(entry)

        assert item.metadata is not None
        assert len(item.metadata["syndication"]) == 2

    def test_includes_in_reply_to_in_metadata(self, microblog_post_factory):
        """Should include in-reply-to in metadata."""
        entry = microblog_post_factory(in_reply_to=["https://example.com/post/123"])

        item = post_to_item(entry)

        assert item.metadata is not None
        assert item.metadata["in_reply_to"] == ["https://example.com/post/123"]

    def test_includes_bookmark_of_in_metadata(self, microblog_post_factory):
        """Should include bookmark-of in metadata."""
        entry = microblog_post_factory(bookmark_of=["https://example.com/article"])

        item = post_to_item(entry)

        assert item.metadata is not None
        assert item.metadata["bookmark_of"] == ["https://example.com/article"]

    def test_includes_like_of_in_metadata(self, microblog_post_factory):
        """Should include like-of in metadata."""
        entry = microblog_post_factory(like_of=["https://example.com/post"])

        item = post_to_item(entry)

        assert item.metadata is not None
        assert item.metadata["like_of"] == ["https://example.com/post"]

    def test_handles_author_as_string(self):
        """Should handle author as plain string."""
        entry = {
            "type": ["h-entry"],
            "properties": {
                "content": ["Test"],
                "url": ["https://example.com/1"],
                "author": ["John Doe"],
            },
        }

        item = post_to_item(entry)

        assert item.author == "John Doe"

    def test_handles_missing_author(self, microblog_post_factory):
        """Should handle missing author gracefully."""
        entry = {
            "type": ["h-entry"],
            "properties": {
                "content": ["Test"],
                "url": ["https://example.com/1"],
            },
        }

        item = post_to_item(entry)

        assert item.author is None

    def test_uses_uid_as_fallback_source_id(self):
        """Should use uid when url is missing."""
        entry = {
            "type": ["h-entry"],
            "properties": {
                "content": ["Test"],
                "uid": ["urn:uuid:12345"],
            },
        }

        item = post_to_item(entry)

        assert item.source_id == "urn:uuid:12345"
