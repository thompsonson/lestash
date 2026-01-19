"""Content extraction from LinkedIn changelog events.

Uses Pydantic schemas for validation and type-safe extraction.
Unknown resource types are preserved with generic content for discovery.
"""

import logging
from datetime import datetime

from lestash.models.item import ItemCreate

from lestash_linkedin.schemas.changelog import ChangelogEvent
from lestash_linkedin.schemas.content_types import (
    CommentActivity,
    InvitationActivity,
    ReactionActivity,
    UgcPostActivity,
)

logger = logging.getLogger(__name__)


def extract_changelog_item(event_data: dict) -> ItemCreate:
    """Extract an ItemCreate from a changelog event.

    Args:
        event_data: Raw changelog event dict from LinkedIn API

    Returns:
        ItemCreate with extracted content and metadata
    """
    # Parse the event envelope
    event = ChangelogEvent.model_validate(event_data)

    # Handle DELETE events (no activity data)
    if event.method == "DELETE":
        return _create_item(
            event=event,
            content=f"Deleted {event.resource_name}",
            author=event.actor,
            created_at=None,
            extra_metadata={},
        )

    # Extract content based on resource type
    if event.resource_name == "ugcPosts":
        return _extract_ugc_post(event)
    elif event.resource_name == "socialActions/comments":
        return _extract_comment(event)
    elif event.resource_name == "socialActions/likes":
        return _extract_reaction(event)
    elif event.resource_name == "invitations":
        return _extract_invitation(event)
    else:
        # Unknown resource type - log for awareness, preserve with generic content
        logger.warning(
            f"Unknown LinkedIn resource type: {event.resource_name}. "
            "Consider adding a schema for this type."
        )
        return _create_item(
            event=event,
            content=f"{event.method} {event.resource_name}",
            author=event.actor,
            created_at=None,
            extra_metadata={},
        )


def _extract_ugc_post(event: ChangelogEvent) -> ItemCreate:
    """Extract content from a ugcPosts event."""
    activity = UgcPostActivity.model_validate(event.activity or {})

    created_at = None
    created_at_ms = activity.get_created_at()
    if created_at_ms:
        created_at = datetime.fromtimestamp(created_at_ms / 1000)

    return _create_item(
        event=event,
        content=activity.get_text(),
        author=activity.author,
        created_at=created_at,
        extra_metadata={
            "media_category": activity.specific_content.get(
                "com.linkedin.ugc.ShareContent", {}
            ).get("shareMediaCategory"),
            "visibility": activity.visibility,
            "lifecycle_state": activity.lifecycle_state,
            "post_id": activity.id,
        },
    )


def _extract_comment(event: ChangelogEvent) -> ItemCreate:
    """Extract content from a socialActions/comments event."""
    activity = CommentActivity.model_validate(event.activity or {})

    created_at = None
    if activity.created and activity.created.get("time"):
        created_at = datetime.fromtimestamp(activity.created["time"] / 1000)

    return _create_item(
        event=event,
        content=activity.get_text(),
        author=activity.actor or activity.author,
        created_at=created_at,
        extra_metadata={"commented_on": activity.object},
    )


def _extract_reaction(event: ChangelogEvent) -> ItemCreate:
    """Extract content from a socialActions/likes event."""
    activity = ReactionActivity.model_validate(event.activity or {})

    created_at = None
    if activity.created and activity.created.get("time"):
        created_at = datetime.fromtimestamp(activity.created["time"] / 1000)

    return _create_item(
        event=event,
        content=f"Reacted with {activity.reaction_type}",
        author=activity.actor,
        created_at=created_at,
        extra_metadata={
            "reaction_type": activity.reaction_type,
            "reacted_to": activity.object,
        },
    )


def _extract_invitation(event: ChangelogEvent) -> ItemCreate:
    """Extract content from an invitations event."""
    activity = InvitationActivity.model_validate(event.activity or {})

    # Build descriptive content
    if activity.message:
        content = activity.message
    elif activity.invitation_type:
        content = f"{event.method} {activity.invitation_type} invitation"
    else:
        content = f"{event.method} invitation"

    return _create_item(
        event=event,
        content=content,
        author=activity.inviter,
        created_at=None,
        extra_metadata={
            "invitation_type": activity.invitation_type,
            "invitee": activity.invitee,
        },
    )


def _create_item(
    event: ChangelogEvent,
    content: str,
    author: str | None,
    created_at: datetime | None,
    extra_metadata: dict,
) -> ItemCreate:
    """Create an ItemCreate with standard fields.

    Args:
        event: The parsed changelog event
        content: Extracted content text
        author: Author URN if available
        created_at: Creation timestamp if available
        extra_metadata: Additional metadata specific to the resource type

    Returns:
        ItemCreate ready for database insertion
    """
    # Fallback to processedAt if no created_at
    if not created_at and event.processed_at:
        created_at = datetime.fromtimestamp(event.processed_at / 1000)

    # Generate unique ID
    event_id = f"changelog-{event.resource_name}-{event.processed_at or hash(str(event))}"

    return ItemCreate(
        source_type="linkedin",
        source_id=event_id,
        content=content or f"{event.method} {event.resource_name}",  # Ensure non-empty
        author=author,
        created_at=created_at,
        is_own_content=True,
        metadata={
            "source": "changelog",
            "resource_name": event.resource_name,
            "method": event.method,
            "status": event.activity_status,
            **{k: v for k, v in extra_metadata.items() if v is not None},
            "raw": event.model_dump(by_alias=True),
        },
    )
