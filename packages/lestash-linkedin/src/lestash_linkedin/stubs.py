"""Stubs for the LinkedIn posts your comments and reactions are on, when LeStash
doesn't hold the post.

See docs/content-and-activity-ontology-design.md, "Stub items for things not held".
``resolve_linkedin_parents`` makes a comment or reaction a child of its post when the
post is held. Otherwise, this pass gives the post a stub item:

- A comment links to its post as ``in_reply_to``.
- Your own reaction is a ``liked`` activity on its post, held or stub, with the reaction
  type in its details. Only the event that made it counts, not a change or a delete.
  Someone else's reaction waits for the people tables, since an activity without an
  actor is yours.
- A comment or reaction on a comment points at the post the comment is on; the
  comment's address is kept as the evidence or in the details.
- Once the post itself is imported, its stub is merged into it, links and activities
  included.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any, NamedTuple

from lestash.core.activities import record_activities
from lestash.core.links import aware_time
from lestash.core.stubs import STUB_PREFIX, make_stub, merge_stub
from lestash.models.item import ActivityCreate

from lestash_linkedin.extractors.changelog import _urn_to_snowflake_ts

ORIGIN = "import:linkedin"
SOURCE = "linkedin"
FEED_URL = "https://www.linkedin.com/feed/update/"
# urn:li:comment:(activity:123,456) is comment 456 on post urn:li:activity:123
COMMENT_URN = re.compile(r"^urn:li:comment:\((\w+):([^,]+),")


class StubCounts(NamedTuple):
    merged: int  # stubs whose post is now held
    links: int  # in_reply_to links added
    activities: int  # liked activities added


def post_urn(target: str) -> str:
    """The post a comment or reaction is on: the post itself, or the post a comment
    target is on."""
    if match := COMMENT_URN.match(target):
        return f"urn:li:{match.group(1)}:{match.group(2)}"
    return target


class _HeldPosts:
    """The held LinkedIn posts, found as resolve_linkedin_parents finds a parent: by
    post_id, by source_id, or by the Snowflake time bucket of the URN."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.by_urn: dict[str, int] = {}
        self.by_time: dict[int, int] = {}
        rows = conn.execute(
            "SELECT id, source_id, json_extract(metadata, '$.post_id'), "
            "json_extract(metadata, '$.snowflake_ts') FROM items "
            "WHERE source_type = 'linkedin' AND held = 1 "
            "AND json_extract(metadata, '$.resource_name') = 'ugcPosts' ORDER BY id DESC"
        )
        # Newest first, so the oldest post is written last and wins a shared key
        for item_id, source_id, post_id, snowflake_ts in rows:
            for urn in (source_id, post_id):
                if urn:
                    self.by_urn[urn] = item_id
            if snowflake_ts is not None:
                self.by_time[int(snowflake_ts)] = item_id

    def find(self, urn: str) -> int | None:
        if urn in self.by_urn:
            return self.by_urn[urn]
        bucket = _urn_to_snowflake_ts(urn)
        return self.by_time.get(bucket) if bucket is not None else None


def _posted_at(urn: str) -> datetime | None:
    """When a post was made, from its URN: a Snowflake id's top bits are Unix ms."""
    try:
        snowflake_id = int(urn.rsplit(":", 1)[-1])
    except ValueError:
        return None
    return datetime.fromtimestamp((snowflake_id >> 22) / 1000, tz=UTC)


def _stub_for(conn: sqlite3.Connection, urn: str) -> int:
    return make_stub(
        conn,
        SOURCE,
        urn,
        url=FEED_URL + urn,
        created_at=_posted_at(urn),
        metadata={"post_urn": urn, "target_snowflake_ts": _urn_to_snowflake_ts(urn)},
    )


def _reaction_time(metadata: dict[str, Any], created_at: Any) -> datetime | None:
    """When a reaction was made: LinkedIn's own time (UTC ms) if stored, else created_at,
    which the changelog extractor writes in local time."""
    raw = metadata.get("raw") or {}
    created = (raw.get("activity") or {}).get("created") or {}
    ms = created.get("time")
    if isinstance(ms, int | float):
        return datetime.fromtimestamp(ms / 1000, tz=UTC)
    return aware_time(created_at, SOURCE) if created_at else None


def _is_own_action(metadata: dict[str, Any]) -> bool:
    raw = metadata.get("raw") or {}
    actor, owner = raw.get("actor"), raw.get("owner")
    return bool(actor) and actor == owner


def _is_created(metadata: dict[str, Any]) -> bool:
    """Whether the changelog event made the reaction, rather than changing or deleting it.
    Only the event that made it is a like; others would add likes that never happened."""
    return metadata.get("method", "CREATE") == "CREATE"


def _merge_filled_stubs(conn: sqlite3.Connection, held: _HeldPosts) -> int:
    merged = 0
    stubs = conn.execute(
        "SELECT id, source_id FROM items WHERE source_type = 'linkedin' AND held = 0 "
        "AND source_id LIKE ?",
        (STUB_PREFIX + "%",),
    ).fetchall()
    for stub_id, source_id in stubs:
        post_id = held.find(source_id.removeprefix(STUB_PREFIX))
        if post_id is not None:
            merge_stub(conn, stub_id, post_id)
            merged += 1
    return merged


def link_linkedin_targets(conn: sqlite3.Connection) -> StubCounts:
    """Merge stubs whose post is now held, then link each comment and reaction to its
    post. Runs after resolve_linkedin_parents; safe to run again. The caller commits."""
    held = _HeldPosts(conn)
    merged = _merge_filled_stubs(conn, held)
    links = activities = 0
    rows = conn.execute(
        "SELECT id, parent_id, created_at, metadata FROM items "
        "WHERE source_type = 'linkedin' AND held = 1 "
        "AND (json_extract(metadata, '$.commented_on') IS NOT NULL "
        "OR json_extract(metadata, '$.reacted_to') IS NOT NULL)"
    ).fetchall()
    for item_id, parent_id, created_at, metadata_json in rows:
        metadata = json.loads(metadata_json)
        commented_on, reacted_to = metadata.get("commented_on"), metadata.get("reacted_to")
        target = commented_on or reacted_to
        if not isinstance(target, str):
            continue
        urn = post_urn(target)

        def post_id(urn: str = urn, parent_id: int | None = parent_id) -> int:
            if parent_id is not None:
                return parent_id
            found = held.find(urn)
            return found if found is not None else _stub_for(conn, urn)

        if commented_on and parent_id is None:
            to_id = post_id()  # first, as making a stub is a change too
            before = conn.total_changes
            conn.execute(
                "INSERT OR IGNORE INTO item_links (from_id, to_id, kind, evidence, origin, "
                "status) VALUES (?, ?, 'in_reply_to', ?, ?, 'confirmed')",
                (item_id, to_id, commented_on, ORIGIN),
            )
            links += conn.total_changes - before
        elif reacted_to and _is_own_action(metadata) and _is_created(metadata):
            when = _reaction_time(metadata, created_at)
            if when is None:
                continue
            details: dict[str, Any] = {}
            if metadata.get("reaction_type"):
                details["reaction"] = metadata["reaction_type"]
            if urn != reacted_to:
                details["on"] = reacted_to
            activities += record_activities(
                conn,
                post_id(),
                [
                    ActivityCreate(
                        verb="liked", occurred_at=when, source=SOURCE, details=details or None
                    )
                ],
            )
    return StubCounts(merged, links, activities)
