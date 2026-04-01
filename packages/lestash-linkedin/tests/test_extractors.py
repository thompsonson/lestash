"""Tests for content extraction from changelog events.

These tests verify the full extraction pipeline from raw API response
to ItemCreate objects.
"""

from lestash.models.item import ItemCreate
from lestash_linkedin.extractors.changelog import extract_changelog_item


class TestUgcPostExtraction:
    """Test extraction of ugcPosts to ItemCreate."""

    def test_extracts_content_from_post(self, ugc_post_event):
        item = extract_changelog_item(ugc_post_event)
        assert isinstance(item, ItemCreate)
        assert "vibe-coded LinkedIn util" in item.content

    def test_extracts_author_urn(self, ugc_post_event):
        item = extract_changelog_item(ugc_post_event)
        assert item.author == "urn:li:person:xu59iSkkD6"

    def test_uses_created_time_not_processed(self, ugc_post_event):
        item = extract_changelog_item(ugc_post_event)
        # created.time (1768818093870) != processedAt (1768818123997)
        # Using pytest.approx for float comparison
        assert item.created_at is not None
        assert int(item.created_at.timestamp() * 1000) == 1768818093870

    def test_sets_source_type(self, ugc_post_event):
        item = extract_changelog_item(ugc_post_event)
        assert item.source_type == "linkedin"

    def test_sets_is_own_content(self, ugc_post_event):
        item = extract_changelog_item(ugc_post_event)
        assert item.is_own_content is True

    def test_includes_resource_name_in_metadata(self, ugc_post_event):
        item = extract_changelog_item(ugc_post_event)
        assert item.metadata["resource_name"] == "ugcPosts"

    def test_includes_method_in_metadata(self, ugc_post_event):
        item = extract_changelog_item(ugc_post_event)
        assert item.metadata["method"] == "CREATE"

    def test_includes_media_category_in_metadata(self, ugc_post_event):
        item = extract_changelog_item(ugc_post_event)
        assert item.metadata["media_category"] == "IMAGE"

    def test_generates_unique_source_id(self, ugc_post_event):
        item = extract_changelog_item(ugc_post_event)
        assert item.source_id is not None
        assert "changelog-ugcPosts-" in item.source_id


class TestCommentExtraction:
    """Test extraction of comments to ItemCreate."""

    def test_extracts_comment_text(self, comment_event):
        item = extract_changelog_item(comment_event)
        assert item.content == "Great post!"

    def test_extracts_comment_text_from_string_message(self, comment_event_string_message):
        item = extract_changelog_item(comment_event_string_message)
        assert item.content == "This is a string message"

    def test_extracts_actor_as_author(self, comment_event):
        item = extract_changelog_item(comment_event)
        assert item.author == "urn:li:person:xu59iSkkD6"

    def test_includes_commented_on_target(self, comment_event):
        item = extract_changelog_item(comment_event)
        assert item.metadata["commented_on"] == "urn:li:activity:123456"

    def test_generates_url_to_commented_post(self, comment_event):
        item = extract_changelog_item(comment_event)
        assert item.url == "https://www.linkedin.com/feed/update/urn:li:activity:123456"


class TestReactionExtraction:
    """Test extraction of reactions to ItemCreate."""

    def test_creates_reaction_content(self, reaction_event):
        item = extract_changelog_item(reaction_event)
        assert item.content == "👍 LIKE on activity:789012"

    def test_extracts_celebrate_reaction(self, reaction_celebrate_event):
        item = extract_changelog_item(reaction_celebrate_event)
        assert item.content == "🎉 CELEBRATE on activity:999888"

    def test_includes_reaction_type_in_metadata(self, reaction_event):
        item = extract_changelog_item(reaction_event)
        assert item.metadata["reaction_type"] == "LIKE"

    def test_includes_target_in_metadata(self, reaction_event):
        item = extract_changelog_item(reaction_event)
        assert item.metadata["reacted_to"] == "urn:li:activity:789012"

    def test_generates_url_to_reacted_post(self, reaction_event):
        item = extract_changelog_item(reaction_event)
        assert item.url == "https://www.linkedin.com/feed/update/urn:li:activity:789012"


class TestInvitationExtraction:
    """Test extraction of invitations to ItemCreate."""

    def test_creates_invitation_content_without_message(self, invitation_event):
        item = extract_changelog_item(invitation_event)
        assert "ACTION" in item.content
        assert "CONNECTION" in item.content
        assert "invitation" in item.content

    def test_uses_message_as_content_when_present(self, invitation_with_message_event):
        item = extract_changelog_item(invitation_with_message_event)
        assert item.content == "Hi, I'd like to connect!"

    def test_extracts_inviter_as_author(self, invitation_event):
        item = extract_changelog_item(invitation_event)
        assert item.author == "urn:li:person:xu59iSkkD6"

    def test_includes_invitee_in_metadata(self, invitation_event):
        item = extract_changelog_item(invitation_event)
        assert item.metadata["invitee"] == "urn:li:person:other123"


class TestDeleteEventExtraction:
    """Test extraction of DELETE events."""

    def test_creates_audit_content_for_delete(self, delete_event):
        item = extract_changelog_item(delete_event)
        assert item.content == "Deleted ugcPosts"

    def test_uses_processed_at_for_timestamp(self, delete_event):
        item = extract_changelog_item(delete_event)
        assert item.created_at is not None
        assert int(item.created_at.timestamp() * 1000) == 1768800000000

    def test_sets_method_to_delete(self, delete_event):
        item = extract_changelog_item(delete_event)
        assert item.metadata["method"] == "DELETE"


class TestUnknownResourceExtraction:
    """Test handling of unknown resource types."""

    def test_preserves_unknown_resource_with_generic_content(self, unknown_resource_event):
        item = extract_changelog_item(unknown_resource_event)
        assert item.content == "CREATE newResourceType"

    def test_includes_resource_name_in_metadata(self, unknown_resource_event):
        item = extract_changelog_item(unknown_resource_event)
        assert item.metadata["resource_name"] == "newResourceType"

    def test_includes_raw_event(self, unknown_resource_event):
        item = extract_changelog_item(unknown_resource_event)
        assert "raw" in item.metadata


class TestMetadataFiltering:
    """Test that None values are filtered from metadata."""

    def test_none_values_not_in_extra_metadata(self, event_with_none_metadata_values):
        """Verify None values from extra_metadata are actually filtered out."""
        item = extract_changelog_item(event_with_none_metadata_values)
        # These should NOT be in metadata because they're None in the source
        assert "media_category" not in item.metadata
        assert "visibility" not in item.metadata
        assert "lifecycle_state" not in item.metadata
        assert "post_id" not in item.metadata

    def test_present_values_are_kept(self, ugc_post_event):
        """Verify that non-None values ARE in metadata."""
        item = extract_changelog_item(ugc_post_event)
        assert item.metadata["media_category"] == "IMAGE"
        assert item.metadata["visibility"] is not None

    def test_raw_event_contains_original_data(self, ugc_post_event):
        """Verify raw contains the actual original event data."""
        item = extract_changelog_item(ugc_post_event)
        assert item.metadata["raw"]["resourceName"] == ugc_post_event["resourceName"]
        assert item.metadata["raw"]["processedAt"] == ugc_post_event["processedAt"]
        assert "activity" in item.metadata["raw"]


class TestSourceIdGeneration:
    """Test source_id uniqueness and format."""

    def test_different_events_get_different_ids(self, ugc_post_event, comment_event):
        item1 = extract_changelog_item(ugc_post_event)
        item2 = extract_changelog_item(comment_event)
        assert item1.source_id != item2.source_id

    def test_same_event_gets_same_id(self, ugc_post_event):
        item1 = extract_changelog_item(ugc_post_event)
        item2 = extract_changelog_item(ugc_post_event)
        assert item1.source_id == item2.source_id

    def test_source_id_includes_resource_name(self, ugc_post_event):
        item = extract_changelog_item(ugc_post_event)
        assert "ugcPosts" in item.source_id

    def test_source_id_uses_resource_id(self, ugc_post_event):
        item = extract_changelog_item(ugc_post_event)
        # Uses resourceId from event envelope for stable dedup
        assert "urn:li:share:7418960805229985792" in item.source_id


class TestContentExtractionBehavior:
    """Test extraction behavior rather than fixture values."""

    def test_extracts_text_from_correct_nested_path(self):
        """Verify text is extracted from the right nested location."""
        event = {
            "resourceName": "ugcPosts",
            "method": "CREATE",
            "processedAt": 1000,
            "activity": {
                "specificContent": {
                    "com.linkedin.ugc.ShareContent": {
                        "shareCommentary": {"text": "EXPECTED_TEXT_12345"}
                    }
                }
            },
        }
        item = extract_changelog_item(event)
        assert item.content == "EXPECTED_TEXT_12345"

    def test_uses_activity_created_time_over_processed_at(self):
        """Verify created.time takes precedence over processedAt."""
        event = {
            "resourceName": "ugcPosts",
            "method": "CREATE",
            "processedAt": 2000000,  # Different from created.time
            "activity": {
                "specificContent": {
                    "com.linkedin.ugc.ShareContent": {"shareCommentary": {"text": "test"}}
                },
                "created": {"time": 1000000},  # Should use this
            },
        }
        item = extract_changelog_item(event)
        assert int(item.created_at.timestamp() * 1000) == 1000000

    def test_falls_back_to_processed_at_when_no_created_time(self):
        """Verify fallback to processedAt when created.time missing."""
        event = {
            "resourceName": "ugcPosts",
            "method": "CREATE",
            "processedAt": 3000000,
            "activity": {
                "specificContent": {
                    "com.linkedin.ugc.ShareContent": {"shareCommentary": {"text": "test"}}
                },
                # No created field
            },
        }
        item = extract_changelog_item(event)
        assert int(item.created_at.timestamp() * 1000) == 3000000

    def test_comment_extracts_from_message_text(self):
        """Verify comment text extraction from message.text."""
        event = {
            "resourceName": "socialActions/comments",
            "method": "CREATE",
            "processedAt": 1000,
            "activity": {"message": {"text": "COMMENT_CONTENT_XYZ"}},
        }
        item = extract_changelog_item(event)
        assert item.content == "COMMENT_CONTENT_XYZ"

    def test_reaction_builds_content_from_type(self):
        """Verify reaction content is built from reactionType with emoji."""
        event = {
            "resourceName": "socialActions/likes",
            "method": "CREATE",
            "processedAt": 1000,
            "activity": {"reactionType": "CELEBRATE"},
        }
        item = extract_changelog_item(event)
        assert item.content == "🎉 CELEBRATE"


class TestEmptyContentFallback:
    """Test behavior when content extraction yields empty string."""

    def test_empty_post_text_falls_back_to_method_resource(self, ugc_post_empty_text_event):
        """Empty shareCommentary.text should fall back to generic content."""
        item = extract_changelog_item(ugc_post_empty_text_event)
        assert item.content == "CREATE ugcPosts"

    def test_missing_share_content_falls_back(self, ugc_post_missing_share_content_event):
        item = extract_changelog_item(ugc_post_missing_share_content_event)
        assert item.content == "CREATE ugcPosts"

    def test_missing_specific_content_falls_back(self, ugc_post_missing_specific_content_event):
        item = extract_changelog_item(ugc_post_missing_specific_content_event)
        assert item.content == "CREATE ugcPosts"

    def test_empty_comment_falls_back(self, comment_empty_message_event):
        item = extract_changelog_item(comment_empty_message_event)
        assert item.content == "CREATE socialActions/comments"


class TestAuthorExtraction:
    """Test author extraction precedence and behavior."""

    def test_ugc_post_uses_activity_author(self):
        """UGC posts should use activity.author."""
        event = {
            "resourceName": "ugcPosts",
            "method": "CREATE",
            "processedAt": 1000,
            "activity": {
                "specificContent": {
                    "com.linkedin.ugc.ShareContent": {"shareCommentary": {"text": "test"}}
                },
                "author": "urn:li:person:post_author",
            },
        }
        item = extract_changelog_item(event)
        assert item.author == "urn:li:person:post_author"

    def test_comment_uses_actor_when_present(self):
        event = {
            "resourceName": "socialActions/comments",
            "method": "CREATE",
            "processedAt": 1000,
            "activity": {"message": "test", "actor": "urn:li:person:actor123"},
        }
        item = extract_changelog_item(event)
        assert item.author == "urn:li:person:actor123"

    def test_comment_falls_back_to_author_field(self):
        event = {
            "resourceName": "socialActions/comments",
            "method": "CREATE",
            "processedAt": 1000,
            "activity": {"message": "test", "author": "urn:li:person:author456"},
        }
        item = extract_changelog_item(event)
        assert item.author == "urn:li:person:author456"

    def test_comment_prefers_actor_over_author(self):
        """When both actor and author present, actor wins."""
        event = {
            "resourceName": "socialActions/comments",
            "method": "CREATE",
            "processedAt": 1000,
            "activity": {
                "message": "test",
                "actor": "urn:li:person:actor",
                "author": "urn:li:person:author",
            },
        }
        item = extract_changelog_item(event)
        assert item.author == "urn:li:person:actor"

    def test_reaction_uses_actor(self):
        event = {
            "resourceName": "socialActions/likes",
            "method": "CREATE",
            "processedAt": 1000,
            "activity": {"reactionType": "LIKE", "actor": "urn:li:person:reactor"},
        }
        item = extract_changelog_item(event)
        assert item.author == "urn:li:person:reactor"

    def test_invitation_uses_inviter(self):
        event = {
            "resourceName": "invitations",
            "method": "ACTION",
            "processedAt": 1000,
            "activity": {"inviter": "urn:li:person:inviter789"},
        }
        item = extract_changelog_item(event)
        assert item.author == "urn:li:person:inviter789"

    def test_delete_event_uses_event_actor(self):
        """DELETE events have no activity, should use event.actor."""
        event = {
            "resourceName": "ugcPosts",
            "method": "DELETE",
            "processedAt": 1000,
            "actor": "urn:li:person:deleter",
            "activity": None,
        }
        item = extract_changelog_item(event)
        assert item.author == "urn:li:person:deleter"


class TestSnowflakeTimestamp:
    """Test Snowflake ID timestamp extraction and matching."""

    def test_post_has_snowflake_ts(self, ugc_post_event):
        item = extract_changelog_item(ugc_post_event)
        assert item.metadata.get("snowflake_ts") is not None
        assert isinstance(item.metadata["snowflake_ts"], int)

    def test_comment_has_target_snowflake_ts(self, comment_event):
        item = extract_changelog_item(comment_event)
        # urn:li:activity:123456 has a tiny ID, ts will be 0
        assert "target_snowflake_ts" in item.metadata

    def test_reaction_has_target_snowflake_ts(self, reaction_event):
        item = extract_changelog_item(reaction_event)
        assert "target_snowflake_ts" in item.metadata

    def test_share_and_activity_urns_match_by_snowflake_ts(self):
        """The key insight: share and activity URNs for the same post
        have the same second-precision Snowflake timestamp."""
        from lestash_linkedin.extractors.changelog import _urn_to_snowflake_ts

        # Real pair from production data
        share_ts = _urn_to_snowflake_ts("urn:li:share:7441773333693493249")
        activity_ts = _urn_to_snowflake_ts("urn:li:activity:7441773334410719232")
        assert share_ts == activity_ts

        # Another real pair
        share_ts2 = _urn_to_snowflake_ts("urn:li:share:7418960805229985792")
        activity_ts2 = _urn_to_snowflake_ts("urn:li:activity:7418960806467436544")
        assert share_ts2 == activity_ts2

    def test_urn_to_snowflake_ts_returns_none_for_invalid(self):
        from lestash_linkedin.extractors.changelog import _urn_to_snowflake_ts

        assert _urn_to_snowflake_ts(None) is None
        assert _urn_to_snowflake_ts("") is None
        assert _urn_to_snowflake_ts("not-a-urn") is None
        assert _urn_to_snowflake_ts("urn:li:groupPost:15875004-7420329640118005760") is None


class TestSourceIdDedup:
    """Test that source_id is stable across CREATE/PARTIAL_UPDATE events."""

    def test_source_id_uses_resource_id(self):
        """CREATE and PARTIAL_UPDATE for the same comment should produce same source_id."""
        create_event = {
            "resourceName": "socialActions/comments",
            "resourceId": "comment-42",
            "method": "CREATE",
            "processedAt": 1000,
            "activity": {"message": "Hello", "object": "urn:li:activity:999"},
        }
        update_event = {
            "resourceName": "socialActions/comments",
            "resourceId": "comment-42",
            "method": "PARTIAL_UPDATE",
            "processedAt": 2000,
            "activity": {"message": "Hello"},
        }
        create_item = extract_changelog_item(create_event)
        update_item = extract_changelog_item(update_event)
        assert create_item.source_id == update_item.source_id

    def test_source_id_falls_back_to_processed_at(self):
        """Events without resourceId use processedAt."""
        event = {
            "resourceName": "ugcPosts",
            "method": "CREATE",
            "processedAt": 5000,
            "activity": {
                "specificContent": {
                    "com.linkedin.ugc.ShareContent": {"shareCommentary": {"text": "test"}}
                }
            },
        }
        item = extract_changelog_item(event)
        assert "5000" in item.source_id
