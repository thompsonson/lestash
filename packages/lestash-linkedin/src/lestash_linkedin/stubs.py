"""Stubs for the LinkedIn posts and comments your comments and reactions are on, when
LeStash doesn't hold them.

See docs/content-and-activity-ontology-design.md, "Stub items for things not held".
``resolve_linkedin_parents`` makes a comment or reaction a child of its post when it
finds the post. This pass runs after it:

- A post named by a URN made within 2 s of a held post's URN (a share and its activity)
  is that post, and becomes the parent. The resolver matches exact URNs only.
- Otherwise the post gets a stub. A comment, the target of a reply or a reaction, gets a
  stub of its own, since LeStash doesn't match comments to the comments it holds. The
  comment's stub replies to its post.
- A comment without a parent links to its target as ``in_reply_to``. A comment with a
  parent has no such link: its parent is its post.
- Your own reaction is a ``liked`` activity on its target (a post, held or stub, or a
  comment's stub) with the reaction type in its details. Only the event that made it
  counts, not a change or a delete. Someone else's reaction waits for the people
  tables, since an activity without an actor is yours.
- Once a post is imported, its stub is merged into it.
"""

from __future__ import annotations

import bisect
import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any, NamedTuple
from urllib.parse import quote

from lestash.core.activities import record_activities
from lestash.core.database import mark_recent_history, max_history_id
from lestash.core.links import aware_time
from lestash.core.stubs import STUB_PREFIX, make_stub, merge_stub
from lestash.models.item import ActivityCreate

ORIGIN = "import:linkedin"
SOURCE = "linkedin"
FEED_URL = "https://www.linkedin.com/feed/update/"
# urn:li:comment:(activity:123,456) is comment 456 on post urn:li:activity:123
COMMENT_URN = re.compile(r"^urn:li:comment:\((\w+):([^,]+),")
# A share and its activity are made within a second of each other; distinct posts
# rarely are. Wider than that, a post is not taken for another.
SAME_POST_MS = 2000


class StubCounts(NamedTuple):
    merged: int  # stubs whose post is now held
    parented: int  # comments and reactions given their held post as parent
    links: int  # in_reply_to links added
    activities: int  # liked activities added


def post_urn(target: str) -> str:
    """The post a comment or reaction is on: the target itself, or the post a comment
    target is on."""
    if match := COMMENT_URN.match(target):
        return f"urn:li:{match.group(1)}:{match.group(2)}"
    return target


def snowflake_ms(urn: str) -> int | None:
    """When a post's URN was made, in Unix ms: a Snowflake id's top bits. A group post's
    id is the part after its group number (urn:li:groupPost:<group>-<id>)."""
    try:
        snowflake_id = int(urn.rsplit(":", 1)[-1].rsplit("-", 1)[-1])
    except ValueError:
        return None
    return snowflake_id >> 22


class _HeldPosts:
    """The held LinkedIn posts, by their URNs and by when their URNs were made."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.by_urn: dict[str, int] = {}
        times: dict[int, set[int]] = {}
        rows = conn.execute(
            "SELECT id, source_id, json_extract(metadata, '$.post_id') FROM items "
            "WHERE source_type = 'linkedin' AND held = 1 "
            "AND json_extract(metadata, '$.resource_name') = 'ugcPosts' ORDER BY id DESC"
        )
        # Newest first, so the oldest post is written last and wins a shared URN
        for item_id, source_id, post_id in rows:
            for urn in (source_id, post_id):
                if not urn:
                    continue
                self.by_urn[urn] = item_id
                if (ms := snowflake_ms(urn)) is not None:
                    times.setdefault(ms, set()).add(item_id)
        self.times = sorted(times.items())
        self.keys = [ms for ms, _ in self.times]

    def find(self, urn: str) -> int | None:
        """The held post with this URN, or the one whose URN was made within SAME_POST_MS
        of it; None if none is, or several URNs are."""
        if urn in self.by_urn:
            return self.by_urn[urn]
        ms = snowflake_ms(urn)
        if ms is None:
            return None
        start = bisect.bisect_left(self.keys, ms - SAME_POST_MS)
        end = bisect.bisect_right(self.keys, ms + SAME_POST_MS)
        near = self.times[start:end]
        found: set[int] = set().union(*(ids for _, ids in near))
        if len(found) == 1 or len(near) == 1:
            # One post, or one URN held twice: the oldest, as an exact match would be
            return min(found) if found else None
        return None


def _posted_at(urn: str) -> datetime | None:
    ms = snowflake_ms(urn)
    return datetime.fromtimestamp(ms / 1000, tz=UTC) if ms is not None else None


def _post_stub(conn: sqlite3.Connection, urn: str) -> int:
    return make_stub(
        conn,
        SOURCE,
        urn,
        url=FEED_URL + urn,
        created_at=_posted_at(urn),
        metadata={"post_urn": urn},
    )


def _comment_stub(conn: sqlite3.Connection, urn: str) -> int:
    post = post_urn(urn)
    return make_stub(
        conn,
        SOURCE,
        urn,
        url=f"{FEED_URL}{post}?commentUrn={quote(urn)}",
        metadata={"post_urn": post, "comment_urn": urn},
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


def _merge_filled_stubs(conn: sqlite3.Connection, held: _HeldPosts) -> tuple[int, list[int]]:
    """Merge each post stub whose post is now held. Returns how many, and the ids of the
    children moved with them."""
    merged, moved = 0, []
    stubs = conn.execute(
        "SELECT id, json_extract(metadata, '$.post_urn') FROM items "
        "WHERE source_type = 'linkedin' AND held = 0 AND source_id LIKE ? "
        "AND json_extract(metadata, '$.comment_urn') IS NULL",
        (STUB_PREFIX + "%",),
    ).fetchall()
    for stub_id, urn in stubs:
        post_id = held.find(urn) if urn else None
        if post_id is not None:
            moved += merge_stub(conn, stub_id, post_id)
            merged += 1
    return merged, moved


class _Pass:
    """One run of link_linkedin_targets: the held posts, and what it added."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.held = _HeldPosts(conn)
        self.links = 0

    def link(self, from_id: int, to_id: int, evidence: str) -> None:
        before = self.conn.total_changes
        self.conn.execute(
            "INSERT OR IGNORE INTO item_links (from_id, to_id, kind, evidence, origin, status) "
            "VALUES (?, ?, 'in_reply_to', ?, ?, 'confirmed')",
            (from_id, to_id, evidence, ORIGIN),
        )
        self.links += self.conn.total_changes - before

    def post(self, urn: str) -> int:
        """The held post, or its stub."""
        found = self.held.find(urn)
        return found if found is not None else _post_stub(self.conn, urn)

    def target(self, urn: str) -> int:
        """The item a comment or reaction is on: a post, held or stub, or a comment's
        stub, which replies to its post."""
        if not COMMENT_URN.match(urn):
            return self.post(urn)
        stub = _comment_stub(self.conn, urn)
        post = post_urn(urn)
        self.link(stub, self.post(post), post)
        return stub


def link_linkedin_targets(conn: sqlite3.Connection) -> StubCounts:
    """Merge stubs whose post is now held, then link each comment and reaction to its
    target. Runs after resolve_linkedin_parents; safe to run again. The caller commits."""
    run = _Pass(conn)
    since = max_history_id(conn)
    merged, touched = _merge_filled_stubs(conn, run.held)
    parented = activities = 0
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
        on_comment = bool(COMMENT_URN.match(target))
        if parent_id is None and not on_comment:
            found = run.held.find(target)
            if found is not None:
                conn.execute("UPDATE items SET parent_id = ? WHERE id = ?", (found, item_id))
                touched.append(item_id)
                parented += 1
                parent_id = found

        if commented_on:
            if parent_id is None:
                run.link(item_id, run.target(target), commented_on)
        elif _is_own_action(metadata) and _is_created(metadata):
            when = _reaction_time(metadata, created_at)
            if when is None:
                continue
            target_id = parent_id if parent_id is not None and not on_comment else None
            reaction = metadata.get("reaction_type")
            activities += record_activities(
                conn,
                target_id if target_id is not None else run.target(target),
                [
                    ActivityCreate(
                        verb="liked",
                        occurred_at=when,
                        source=SOURCE,
                        details={"reaction": reaction} if reaction else None,
                    )
                ],
            )
    # A comment's parent is its post: a reply link to the parent says it twice. It is
    # left by a stub merged into the post, whatever the order the two were imported in.
    conn.execute(
        "DELETE FROM item_links WHERE kind = 'in_reply_to' AND origin = ? "
        "AND to_id = (SELECT parent_id FROM items WHERE items.id = item_links.from_id)",
        (ORIGIN,),
    )
    mark_recent_history(conn, since, "sync", item_ids=touched)
    return StubCounts(merged, parented, run.links, activities)
