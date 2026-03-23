"""Shared display/enrichment helpers for items.

These functions enrich Item objects with resolved author names, subtypes,
and preview text. Used by both the CLI and the API server.
"""

import sqlite3

from lestash.core.database import get_person_profile, get_post_cache
from lestash.models.item import Item


def resolve_author(conn: sqlite3.Connection, author: str | None) -> str:
    """Resolve author URN to display name if available."""
    if not author:
        return "-"
    profile = get_person_profile(conn, author)
    if profile and profile.get("display_name"):
        return profile["display_name"]
    return author


def get_author_actor(conn: sqlite3.Connection, item: Item) -> tuple[str, str]:
    """Extract author and actor for display.

    For reactions:
    - Author = who wrote the content being reacted to
    - Actor = who made the reaction

    Returns:
        Tuple of (author_display, actor_display)
    """
    raw = item.metadata.get("raw", {}) if item.metadata else {}
    owner_urn = raw.get("owner")
    actor_urn = raw.get("actor")

    # Resolve actor URN
    actor_display = resolve_author(conn, actor_urn) if actor_urn else "-"

    # Determine author based on scenario
    if owner_urn and actor_urn and owner_urn != actor_urn:
        # Someone else reacted to YOUR content
        author_display = "You"
    else:
        # You reacted to someone else's content
        # Try to get author from post_cache enrichment
        target_urn = (
            item.metadata.get("reacted_to") or item.metadata.get("commented_on")
            if item.metadata
            else None
        )
        if target_urn:
            cached = get_post_cache(conn, target_urn)
            author_display = cached["author_name"] if cached and cached.get("author_name") else "-"
        else:
            # Not a reaction/comment - fall back to item.author
            author_display = resolve_author(conn, item.author)

    return author_display, actor_display


def get_item_subtype(item: Item) -> str:
    """Derive a descriptive type from source_type and metadata.

    Returns format: source/subtype, e.g.:
    - linkedin/post
    - linkedin/comment
    - linkedin/reaction/like (→post)
    - linkedin/invitation
    - bluesky (no subtype available)
    """
    source = item.source_type

    if not item.metadata:
        return source

    resource = item.metadata.get("resource_name", "")
    reaction_type = item.metadata.get("reaction_type", "")
    reacted_to = item.metadata.get("reacted_to", "")

    subtype = None
    if resource == "ugcPosts":
        subtype = "post"
    elif resource == "socialActions/comments":
        subtype = "comment"
    elif resource == "socialActions/likes":
        reaction_label = reaction_type.lower() if reaction_type else "like"
        if reacted_to:
            target = "comment" if "comment:" in reacted_to else "post"
            subtype = f"reaction/{reaction_label} (→{target})"
        else:
            subtype = f"reaction/{reaction_label}"
    elif resource == "invitations":
        subtype = "invitation"
    elif resource == "messages":
        subtype = "message"
    elif resource:
        subtype = resource.replace("socialActions/", "")

    if subtype:
        return f"{source}/{subtype}"
    return source


def get_preview(conn: sqlite3.Connection, item: Item, max_length: int = 50) -> str:
    """Get preview text for display.

    For reactions (LIKE, CELEBRATE, etc.) and comments, if we have cached
    content for the target, show that content instead of the activity URN.
    """
    # Check if this is a reaction or comment with a target
    if item.metadata:
        target_urn = item.metadata.get("reacted_to") or item.metadata.get("commented_on")
        if target_urn:
            # Detect if target is a comment or post
            is_comment_target = target_urn.startswith("urn:li:comment:")

            # Look up cached content
            cached = get_post_cache(conn, target_urn)
            if cached and cached.get("content_preview"):
                # Extract emoji and reaction type from original content
                # Format: "👍 LIKE on activity:123" -> "👍 LIKE"
                original = item.content
                parts = original.split(" on ")
                prefix = parts[0] if parts else ""

                # Add "(comment)" indicator for comment reactions
                if is_comment_target:
                    prefix = f"{prefix} (comment)"

                # Check if this is a reaction from someone else (has reactor_name)
                if cached.get("reactor_name"):
                    prefix = f"{prefix} from {cached['reactor_name']}"

                # Build new preview with cached content
                cached_preview = cached["content_preview"][:max_length]
                if len(cached["content_preview"]) > max_length:
                    cached_preview += "..."

                return f'{prefix}: "{cached_preview}"'

            # Not enriched but is a comment reaction - add indicator to original content
            if is_comment_target and " on " in item.content:
                parts = item.content.split(" on ", 1)
                if len(parts) == 2:
                    preview = f"{parts[0]} (comment) on {parts[1]}"
                    if len(preview) > max_length:
                        return preview[:max_length] + "..."
                    return preview

    # Fall back to standard preview
    if item.title:
        return item.title
    elif len(item.content) > max_length:
        return item.content[:max_length] + "..."
    else:
        return item.content
