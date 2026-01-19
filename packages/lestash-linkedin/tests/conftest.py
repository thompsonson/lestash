"""Test fixtures with real LinkedIn API payloads.

These payloads are captured from actual API responses. When LinkedIn
changes their schema, update these fixtures and the corresponding schemas.
"""

import pytest


@pytest.fixture
def ugc_post_event() -> dict:
    """Sample ugcPosts changelog event.

    Based on real API response for a post with an image.
    """
    return {
        "resourceName": "ugcPosts",
        "method": "CREATE",
        "processedAt": 1768818123997,
        "activityStatus": "SUCCESS",
        "owner": "urn:li:person:xu59iSkkD6",
        "activity": {
            "lifecycleState": "PUBLISHED",
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {
                        "attributes": [],
                        "text": "I've just found this in a vibe-coded LinkedIn util...",
                    },
                    "shareMediaCategory": "IMAGE",
                    "media": [{"media": "urn:li:digitalmediaAsset:D4D22AQHodZB6deMbMg"}],
                }
            },
            "author": "urn:li:person:xu59iSkkD6",
            "created": {"actor": "urn:li:person:xu59iSkkD6", "time": 1768818093870},
            "id": "urn:li:share:7418960805229985792",
        },
    }


@pytest.fixture
def ugc_post_text_only_event() -> dict:
    """Sample ugcPosts changelog event for text-only post."""
    return {
        "resourceName": "ugcPosts",
        "method": "CREATE",
        "processedAt": 1768800000000,
        "activityStatus": "SUCCESS",
        "activity": {
            "lifecycleState": "PUBLISHED",
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {
                        "attributes": [],
                        "text": "Just a simple text post without media.",
                    },
                    "shareMediaCategory": "NONE",
                }
            },
            "author": "urn:li:person:abc123",
            "created": {"time": 1768799990000},
            "id": "urn:li:share:1234567890",
        },
    }


@pytest.fixture
def comment_event() -> dict:
    """Sample socialActions/comments changelog event."""
    return {
        "resourceName": "socialActions/comments",
        "method": "CREATE",
        "processedAt": 1768800000000,
        "activity": {
            "message": {"text": "Great post!"},
            "actor": "urn:li:person:xu59iSkkD6",
            "object": "urn:li:activity:123456",
        },
    }


@pytest.fixture
def comment_event_string_message() -> dict:
    """Sample comment event where message is a string, not dict."""
    return {
        "resourceName": "socialActions/comments",
        "method": "CREATE",
        "processedAt": 1768800000000,
        "activity": {
            "message": "This is a string message",
            "actor": "urn:li:person:xyz789",
            "object": "urn:li:activity:654321",
        },
    }


@pytest.fixture
def reaction_event() -> dict:
    """Sample socialActions/likes changelog event."""
    return {
        "resourceName": "socialActions/likes",
        "method": "CREATE",
        "processedAt": 1768800000000,
        "activity": {
            "reactionType": "LIKE",
            "actor": "urn:li:person:xu59iSkkD6",
            "object": "urn:li:activity:789012",
        },
    }


@pytest.fixture
def reaction_celebrate_event() -> dict:
    """Sample reaction with CELEBRATE type."""
    return {
        "resourceName": "socialActions/likes",
        "method": "CREATE",
        "processedAt": 1768800000000,
        "activity": {
            "reactionType": "CELEBRATE",
            "actor": "urn:li:person:abc123",
            "object": "urn:li:activity:999888",
        },
    }


@pytest.fixture
def invitation_event() -> dict:
    """Sample invitations changelog event."""
    return {
        "resourceName": "invitations",
        "method": "ACTION",
        "processedAt": 1768800000000,
        "activity": {
            "invitationType": "CONNECTION",
            "inviter": "urn:li:person:xu59iSkkD6",
            "invitee": "urn:li:person:other123",
        },
    }


@pytest.fixture
def invitation_with_message_event() -> dict:
    """Sample invitation with a custom message."""
    return {
        "resourceName": "invitations",
        "method": "ACTION",
        "processedAt": 1768800000000,
        "activity": {
            "message": "Hi, I'd like to connect!",
            "invitationType": "CONNECTION",
            "inviter": "urn:li:person:abc123",
            "invitee": "urn:li:person:def456",
        },
    }


@pytest.fixture
def delete_event() -> dict:
    """Sample DELETE event (no activity data)."""
    return {
        "resourceName": "ugcPosts",
        "method": "DELETE",
        "processedAt": 1768800000000,
        "activity": None,
    }


@pytest.fixture
def unknown_resource_event() -> dict:
    """Sample event with unknown resource type."""
    return {
        "resourceName": "newResourceType",
        "method": "CREATE",
        "processedAt": 1768800000000,
        "activity": {"some": "data"},
    }


# Edge case fixtures for testing fallback behavior


@pytest.fixture
def ugc_post_empty_text_event() -> dict:
    """Post with empty shareCommentary text."""
    return {
        "resourceName": "ugcPosts",
        "method": "CREATE",
        "processedAt": 1768800000000,
        "activity": {
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": ""},
                    "shareMediaCategory": "IMAGE",
                }
            },
            "author": "urn:li:person:abc123",
        },
    }


@pytest.fixture
def ugc_post_missing_share_content_event() -> dict:
    """Post where specificContent exists but ShareContent key is missing."""
    return {
        "resourceName": "ugcPosts",
        "method": "CREATE",
        "processedAt": 1768800000000,
        "activity": {
            "specificContent": {},  # Missing com.linkedin.ugc.ShareContent
            "author": "urn:li:person:abc123",
        },
    }


@pytest.fixture
def ugc_post_missing_specific_content_event() -> dict:
    """Post with no specificContent at all."""
    return {
        "resourceName": "ugcPosts",
        "method": "CREATE",
        "processedAt": 1768800000000,
        "activity": {
            "author": "urn:li:person:abc123",
        },
    }


@pytest.fixture
def comment_empty_message_event() -> dict:
    """Comment with empty message."""
    return {
        "resourceName": "socialActions/comments",
        "method": "CREATE",
        "processedAt": 1768800000000,
        "activity": {
            "message": {"text": ""},
            "actor": "urn:li:person:abc123",
        },
    }


@pytest.fixture
def event_with_none_metadata_values() -> dict:
    """Event that would produce None values in metadata.

    Used to test that None values are filtered out of extra_metadata.
    """
    return {
        "resourceName": "ugcPosts",
        "method": "CREATE",
        "processedAt": 1768800000000,
        "activity": {
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": "Test post"},
                    # No shareMediaCategory - should be None
                }
            },
            # No visibility, no lifecycleState, no id - all should be None
            "author": "urn:li:person:abc123",
        },
    }
