"""Tests for LinkedIn API schema validation.

These tests ensure our Pydantic models correctly parse LinkedIn API responses.
When LinkedIn changes their API, these tests will fail first.
"""

import pytest
from lestash_linkedin.schemas.changelog import ChangelogEvent
from lestash_linkedin.schemas.content_types import (
    CommentActivity,
    InvitationActivity,
    ReactionActivity,
    UgcPostActivity,
)
from pydantic import ValidationError


class TestChangelogEventSchema:
    """Test ChangelogEvent parsing."""

    def test_parses_ugc_post_event(self, ugc_post_event):
        event = ChangelogEvent.model_validate(ugc_post_event)
        assert event.resource_name == "ugcPosts"
        assert event.method == "CREATE"
        assert event.processed_at == 1768818123997
        assert event.activity is not None

    def test_parses_delete_event_without_activity(self, delete_event):
        event = ChangelogEvent.model_validate(delete_event)
        assert event.method == "DELETE"
        assert event.activity is None

    def test_parses_action_method(self, invitation_event):
        event = ChangelogEvent.model_validate(invitation_event)
        assert event.method == "ACTION"

    def test_requires_resource_name(self):
        with pytest.raises(ValidationError):
            ChangelogEvent.model_validate({"method": "CREATE"})

    def test_requires_method(self):
        with pytest.raises(ValidationError):
            ChangelogEvent.model_validate({"resourceName": "ugcPosts"})

    def test_validates_method_literal(self):
        with pytest.raises(ValidationError):
            ChangelogEvent.model_validate(
                {
                    "resourceName": "ugcPosts",
                    "method": "INVALID_METHOD",
                }
            )

    def test_accepts_all_valid_methods(self):
        for method in ["CREATE", "UPDATE", "PARTIAL_UPDATE", "DELETE", "ACTION"]:
            event = ChangelogEvent.model_validate(
                {
                    "resourceName": "test",
                    "method": method,
                }
            )
            assert event.method == method


class TestUgcPostActivity:
    """Test UGC post activity parsing."""

    def test_extracts_post_text(self, ugc_post_event):
        activity = UgcPostActivity.model_validate(ugc_post_event["activity"])
        text = activity.get_text()
        assert "vibe-coded LinkedIn util" in text

    def test_extracts_author_urn(self, ugc_post_event):
        activity = UgcPostActivity.model_validate(ugc_post_event["activity"])
        assert activity.author == "urn:li:person:xu59iSkkD6"

    def test_extracts_created_time(self, ugc_post_event):
        activity = UgcPostActivity.model_validate(ugc_post_event["activity"])
        assert activity.get_created_at() == 1768818093870

    def test_handles_empty_activity(self):
        activity = UgcPostActivity.model_validate({})
        assert activity.get_text() == ""
        assert activity.get_created_at() is None

    def test_extracts_visibility(self, ugc_post_event):
        activity = UgcPostActivity.model_validate(ugc_post_event["activity"])
        assert activity.visibility is not None
        assert "com.linkedin.ugc.MemberNetworkVisibility" in activity.visibility

    def test_extracts_lifecycle_state(self, ugc_post_event):
        activity = UgcPostActivity.model_validate(ugc_post_event["activity"])
        assert activity.lifecycle_state == "PUBLISHED"


class TestCommentActivity:
    """Test comment activity parsing."""

    def test_extracts_comment_text_from_dict(self, comment_event):
        activity = CommentActivity.model_validate(comment_event["activity"])
        assert activity.get_text() == "Great post!"

    def test_extracts_comment_text_from_string(self, comment_event_string_message):
        activity = CommentActivity.model_validate(comment_event_string_message["activity"])
        assert activity.get_text() == "This is a string message"

    def test_extracts_actor(self, comment_event):
        activity = CommentActivity.model_validate(comment_event["activity"])
        assert activity.actor == "urn:li:person:xu59iSkkD6"

    def test_extracts_object(self, comment_event):
        activity = CommentActivity.model_validate(comment_event["activity"])
        assert activity.object == "urn:li:activity:123456"

    def test_handles_empty_message(self):
        activity = CommentActivity.model_validate({})
        assert activity.get_text() == ""


class TestReactionActivity:
    """Test reaction activity parsing."""

    def test_extracts_reaction_type(self, reaction_event):
        activity = ReactionActivity.model_validate(reaction_event["activity"])
        assert activity.reaction_type == "LIKE"

    def test_extracts_celebrate_reaction(self, reaction_celebrate_event):
        activity = ReactionActivity.model_validate(reaction_celebrate_event["activity"])
        assert activity.reaction_type == "CELEBRATE"

    def test_defaults_to_like(self):
        activity = ReactionActivity.model_validate({})
        assert activity.reaction_type == "LIKE"

    def test_extracts_target_object(self, reaction_event):
        activity = ReactionActivity.model_validate(reaction_event["activity"])
        assert activity.object == "urn:li:activity:789012"


class TestInvitationActivity:
    """Test invitation activity parsing."""

    def test_extracts_invitation_type(self, invitation_event):
        activity = InvitationActivity.model_validate(invitation_event["activity"])
        assert activity.invitation_type == "CONNECTION"

    def test_extracts_inviter_invitee(self, invitation_event):
        activity = InvitationActivity.model_validate(invitation_event["activity"])
        assert activity.inviter == "urn:li:person:xu59iSkkD6"
        assert activity.invitee == "urn:li:person:other123"

    def test_extracts_message(self, invitation_with_message_event):
        activity = InvitationActivity.model_validate(invitation_with_message_event["activity"])
        assert activity.message == "Hi, I'd like to connect!"


class TestUgcPostActivityEdgeCases:
    """Test UGC post activity with edge cases."""

    def test_get_text_returns_empty_for_missing_specific_content(self):
        """Verify graceful handling when specificContent is missing."""
        activity = UgcPostActivity.model_validate({"author": "urn:li:person:abc"})
        assert activity.get_text() == ""

    def test_get_text_returns_empty_for_missing_share_content_key(self):
        """Verify handling when ShareContent key is missing."""
        activity = UgcPostActivity.model_validate({"specificContent": {"some.other.type": {}}})
        assert activity.get_text() == ""

    def test_get_text_returns_empty_for_missing_commentary(self):
        """Verify handling when shareCommentary is missing."""
        activity = UgcPostActivity.model_validate(
            {"specificContent": {"com.linkedin.ugc.ShareContent": {}}}
        )
        assert activity.get_text() == ""

    def test_get_text_returns_empty_for_empty_text(self):
        """Verify handling when text is empty string."""
        activity = UgcPostActivity.model_validate(
            {
                "specificContent": {
                    "com.linkedin.ugc.ShareContent": {"shareCommentary": {"text": ""}}
                }
            }
        )
        assert activity.get_text() == ""

    def test_get_created_at_returns_none_for_missing_created(self):
        activity = UgcPostActivity.model_validate({})
        assert activity.get_created_at() is None

    def test_get_created_at_returns_none_for_missing_time(self):
        activity = UgcPostActivity.model_validate({"created": {}})
        assert activity.get_created_at() is None

    def test_get_created_at_returns_none_for_empty_created(self):
        activity = UgcPostActivity.model_validate({"created": {"actor": "urn:li:person:abc"}})
        assert activity.get_created_at() is None


class TestCommentActivityEdgeCases:
    """Test comment activity edge cases."""

    def test_get_text_with_empty_dict_message(self):
        activity = CommentActivity.model_validate({"message": {}})
        assert activity.get_text() == ""

    def test_get_text_with_none_message(self):
        activity = CommentActivity.model_validate({"message": None})
        assert activity.get_text() == ""

    def test_get_text_with_empty_string_message(self):
        activity = CommentActivity.model_validate({"message": ""})
        assert activity.get_text() == ""

    def test_get_text_with_dict_missing_text_key(self):
        activity = CommentActivity.model_validate({"message": {"other": "value"}})
        assert activity.get_text() == ""


class TestReactionActivityEdgeCases:
    """Test reaction activity edge cases."""

    def test_defaults_to_like_when_no_reaction_type(self):
        activity = ReactionActivity.model_validate({})
        assert activity.reaction_type == "LIKE"

    def test_accepts_various_reaction_types(self):
        """LinkedIn has multiple reaction types."""
        for reaction in ["LIKE", "CELEBRATE", "SUPPORT", "LOVE", "INSIGHTFUL", "FUNNY"]:
            activity = ReactionActivity.model_validate({"reactionType": reaction})
            assert activity.reaction_type == reaction
