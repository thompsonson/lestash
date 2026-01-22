"""Tests for YouTube video extraction and transformation."""

from lestash.models.item import ItemCreate
from lestash_youtube.source import (
    parse_iso8601_duration,
    subscription_to_item,
    video_to_item,
)


class TestParseIso8601Duration:
    """Test ISO 8601 duration parsing."""

    def test_parses_hours_minutes_seconds(self):
        """Should parse full duration with hours, minutes, and seconds."""
        result = parse_iso8601_duration("PT1H30M45S")
        assert result == 1 * 3600 + 30 * 60 + 45

    def test_parses_minutes_and_seconds(self):
        """Should parse duration with minutes and seconds."""
        result = parse_iso8601_duration("PT4M35S")
        assert result == 4 * 60 + 35

    def test_parses_seconds_only(self):
        """Should parse duration with only seconds."""
        result = parse_iso8601_duration("PT59S")
        assert result == 59

    def test_parses_minutes_only(self):
        """Should parse duration with only minutes."""
        result = parse_iso8601_duration("PT10M")
        assert result == 10 * 60

    def test_parses_hours_only(self):
        """Should parse duration with only hours."""
        result = parse_iso8601_duration("PT2H")
        assert result == 2 * 3600

    def test_parses_hours_and_seconds(self):
        """Should parse duration with hours and seconds, no minutes."""
        result = parse_iso8601_duration("PT1H30S")
        assert result == 1 * 3600 + 30

    def test_returns_none_for_none_input(self):
        """Should return None for None input."""
        result = parse_iso8601_duration(None)
        assert result is None

    def test_returns_none_for_empty_string(self):
        """Should return None for empty string."""
        result = parse_iso8601_duration("")
        assert result is None

    def test_returns_none_for_invalid_format(self):
        """Should return None for invalid duration format."""
        result = parse_iso8601_duration("invalid")
        assert result is None

    def test_returns_none_for_missing_pt_prefix(self):
        """Should return None when PT prefix is missing."""
        result = parse_iso8601_duration("1H30M")
        assert result is None

    def test_parses_zero_duration(self):
        """Should handle zero seconds."""
        result = parse_iso8601_duration("PT0S")
        assert result == 0


class TestVideoToItem:
    """Test video data to ItemCreate conversion."""

    def test_converts_basic_video(self, youtube_video_factory):
        """Should convert video data to ItemCreate with correct fields."""
        video = youtube_video_factory(
            video_id="abc123",
            title="My Test Video",
            description="This is a test video description",
            channel_title="Test Channel",
        )

        item = video_to_item(video, "liked")

        assert isinstance(item, ItemCreate)
        assert item.source_type == "youtube"
        assert item.source_id == "liked:abc123"
        assert item.title == "My Test Video"
        assert item.content == "This is a test video description"
        assert item.author == "Test Channel"

    def test_builds_youtube_url(self, youtube_video_factory):
        """Should construct YouTube watch URL from video ID."""
        video = youtube_video_factory(video_id="dQw4w9WgXcQ")

        item = video_to_item(video, "liked")

        assert item.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_parses_published_timestamp(self, youtube_video_factory):
        """Should parse ISO 8601 timestamp to datetime."""
        video = youtube_video_factory(published_at="2025-01-15T10:30:45Z")

        item = video_to_item(video, "liked")

        assert item.created_at is not None
        assert item.created_at.year == 2025
        assert item.created_at.month == 1
        assert item.created_at.day == 15
        assert item.created_at.hour == 10
        assert item.created_at.minute == 30

    def test_sets_is_own_content_false(self, youtube_video_factory):
        """Should set is_own_content to False for liked/watched videos."""
        video = youtube_video_factory()

        item = video_to_item(video, "liked")

        assert item.is_own_content is False

    def test_includes_source_subtype_in_metadata(self, youtube_video_factory):
        """Should store source_subtype in metadata."""
        video = youtube_video_factory()

        liked_item = video_to_item(video, "liked")
        history_item = video_to_item(video, "history")

        assert liked_item.metadata["source_subtype"] == "liked"
        assert history_item.metadata["source_subtype"] == "history"

    def test_stores_duration_in_metadata(self, youtube_video_factory):
        """Should store duration in both seconds and ISO format."""
        video = youtube_video_factory(duration="PT1H23M45S")

        item = video_to_item(video, "liked")

        assert item.metadata["duration_seconds"] == 1 * 3600 + 23 * 60 + 45
        assert item.metadata["duration_iso"] == "PT1H23M45S"

    def test_stores_statistics_in_metadata(self, youtube_video_factory):
        """Should store view, like, and comment counts."""
        video = youtube_video_factory(
            view_count="1000000",
            like_count="50000",
            comment_count="5000",
        )

        item = video_to_item(video, "liked")

        assert item.metadata["view_count"] == 1000000
        assert item.metadata["like_count"] == 50000
        assert item.metadata["comment_count"] == 5000

    def test_stores_channel_info_in_metadata(self, youtube_video_factory):
        """Should store channel ID and title in metadata."""
        video = youtube_video_factory(
            channel_id="UC_test_channel",
            channel_title="My Channel",
        )

        item = video_to_item(video, "liked")

        assert item.metadata["channel_id"] == "UC_test_channel"
        assert item.metadata["channel_title"] == "My Channel"

    def test_stores_thumbnail_url(self, youtube_video_factory):
        """Should store best available thumbnail URL."""
        video = youtube_video_factory(
            thumbnails={
                "default": {"url": "https://example.com/default.jpg"},
                "high": {"url": "https://example.com/high.jpg"},
            }
        )

        item = video_to_item(video, "liked")

        assert item.metadata["thumbnail_url"] == "https://example.com/high.jpg"

    def test_prefers_maxres_thumbnail(self, youtube_video_factory):
        """Should prefer maxres thumbnail when available."""
        video = youtube_video_factory(
            thumbnails={
                "default": {"url": "https://example.com/default.jpg"},
                "high": {"url": "https://example.com/high.jpg"},
                "maxres": {"url": "https://example.com/maxres.jpg"},
            }
        )

        item = video_to_item(video, "liked")

        assert item.metadata["thumbnail_url"] == "https://example.com/maxres.jpg"

    def test_stores_tags_in_metadata(self, youtube_video_factory):
        """Should store video tags."""
        video = youtube_video_factory(tags=["python", "tutorial", "coding"])

        item = video_to_item(video, "liked")

        assert item.metadata["tags"] == ["python", "tutorial", "coding"]

    def test_stores_definition_in_metadata(self, youtube_video_factory):
        """Should store video definition (hd/sd)."""
        video = youtube_video_factory(definition="hd")

        item = video_to_item(video, "liked")

        assert item.metadata["definition"] == "hd"

    def test_stores_category_id(self, youtube_video_factory):
        """Should store YouTube category ID."""
        video = youtube_video_factory(category_id="28")

        item = video_to_item(video, "liked")

        assert item.metadata["category_id"] == "28"


class TestVideoToItemHistorySpecific:
    """Test history-specific video conversion."""

    def test_stores_watched_at_timestamp(self, youtube_history_video_factory):
        """Should store watched_at timestamp for history videos."""
        video = youtube_history_video_factory(watched_at="2025-01-20T19:30:00Z")

        item = video_to_item(video, "history")

        assert item.metadata["watched_at"] == "2025-01-20T19:30:00Z"

    def test_uses_history_source_id_prefix(self, youtube_history_video_factory):
        """Should use history prefix in source_id."""
        video = youtube_history_video_factory(video_id="hist_vid_123")

        item = video_to_item(video, "history")

        assert item.source_id == "history:hist_vid_123"


class TestVideoToItemEdgeCases:
    """Test edge cases in video conversion."""

    def test_handles_missing_description(self, youtube_video_factory):
        """Should handle videos without description."""
        video = youtube_video_factory()
        video["description"] = None

        item = video_to_item(video, "liked")

        assert item.content == ""

    def test_handles_empty_description(self, youtube_video_factory):
        """Should handle empty description string."""
        video = youtube_video_factory(description="")

        item = video_to_item(video, "liked")

        assert item.content == ""

    def test_handles_missing_duration(self, youtube_video_factory):
        """Should handle missing duration gracefully."""
        video = youtube_video_factory()
        video["duration"] = None

        item = video_to_item(video, "liked")

        assert item.metadata["duration_seconds"] is None
        assert item.metadata["duration_iso"] is None

    def test_handles_missing_statistics(self, youtube_video_factory):
        """Should handle missing statistics."""
        video = youtube_video_factory()
        video["view_count"] = None
        video["like_count"] = None
        video["comment_count"] = None

        item = video_to_item(video, "liked")

        assert "view_count" not in item.metadata
        assert "like_count" not in item.metadata
        assert "comment_count" not in item.metadata

    def test_handles_missing_thumbnails(self, youtube_video_factory):
        """Should handle missing thumbnails."""
        video = youtube_video_factory(thumbnails={})

        item = video_to_item(video, "liked")

        assert item.metadata["thumbnail_url"] is None

    def test_handles_invalid_timestamp(self, youtube_video_factory):
        """Should handle invalid timestamp gracefully."""
        video = youtube_video_factory(published_at="invalid-date")

        item = video_to_item(video, "liked")

        assert item.created_at is None

    def test_handles_missing_video_id(self, youtube_video_factory):
        """Should handle missing video ID."""
        video = youtube_video_factory()
        video["id"] = None

        item = video_to_item(video, "liked")

        assert item.url is None
        assert item.source_id == "liked:None"


class TestSubscriptionToItem:
    """Test subscription data to ItemCreate conversion."""

    def test_converts_basic_subscription(self, youtube_subscription_factory):
        """Should convert subscription data to ItemCreate."""
        sub = youtube_subscription_factory(
            channel_id="UC_test_channel",
            title="My Favorite Channel",
            description="Great content about testing",
        )

        item = subscription_to_item(sub)

        assert isinstance(item, ItemCreate)
        assert item.source_type == "youtube"
        assert item.source_id == "subscription:UC_test_channel"
        assert item.title == "My Favorite Channel"
        assert item.content == "Great content about testing"
        assert item.author == "My Favorite Channel"

    def test_builds_channel_url(self, youtube_subscription_factory):
        """Should construct YouTube channel URL."""
        sub = youtube_subscription_factory(channel_id="UC_abc123")

        item = subscription_to_item(sub)

        assert item.url == "https://www.youtube.com/channel/UC_abc123"

    def test_sets_source_subtype(self, youtube_subscription_factory):
        """Should set source_subtype to subscription."""
        sub = youtube_subscription_factory()

        item = subscription_to_item(sub)

        assert item.metadata["source_subtype"] == "subscription"

    def test_stores_subscription_id(self, youtube_subscription_factory):
        """Should store subscription ID in metadata."""
        sub = youtube_subscription_factory(subscription_id="sub_xyz789")

        item = subscription_to_item(sub)

        assert item.metadata["subscription_id"] == "sub_xyz789"

    def test_stores_thumbnail_url(self, youtube_subscription_factory):
        """Should store channel thumbnail URL."""
        sub = youtube_subscription_factory(
            thumbnails={"high": {"url": "https://example.com/channel_thumb.jpg"}}
        )

        item = subscription_to_item(sub)

        assert item.metadata["thumbnail_url"] == "https://example.com/channel_thumb.jpg"

    def test_parses_subscription_date(self, youtube_subscription_factory):
        """Should parse subscription timestamp."""
        sub = youtube_subscription_factory(published_at="2024-06-15T08:00:00Z")

        item = subscription_to_item(sub)

        assert item.created_at is not None
        assert item.created_at.year == 2024
        assert item.created_at.month == 6

    def test_sets_is_own_content_false(self, youtube_subscription_factory):
        """Should set is_own_content to False."""
        sub = youtube_subscription_factory()

        item = subscription_to_item(sub)

        assert item.is_own_content is False

    def test_handles_missing_description(self, youtube_subscription_factory):
        """Should handle missing description."""
        sub = youtube_subscription_factory()
        sub["description"] = None

        item = subscription_to_item(sub)

        assert item.content == ""
