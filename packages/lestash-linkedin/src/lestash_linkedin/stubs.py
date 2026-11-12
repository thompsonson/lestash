"""Stubs for the LinkedIn posts and comments your comments and reactions are on, when
LeStash doesn't hold them.

See docs/content-and-activity-ontology-design.md, "Stub items for things not held".
``resolve_linkedin_parents`` makes a comment or reaction a child of its post when it
finds the post. This pass runs after it:

- A post named by a URN made within 2 s of a held post's URN (a share and its activity)
  is that post, and becomes the parent. The resolver matches exact URNs only.
- Otherwise the post gets a stub. A comment, the target of a reply or a reaction, is
  the held comment with its comment id, else gets a stub of its own, which replies to
  its post.
- A comment without a parent links to its target as ``in_reply_to``. A comment with a
  parent has no such link: its parent is its post.
- A reaction is a ``liked`` activity on its target (a post, held or stub, or a
  comment's stub) with the reaction type in its details. Only the event that made it
  counts, not a change or a delete. Someone else's has their person as its actor,
  their LinkedIn account made if not yet seen; yours has none.
- Once a post or comment is imported, its stub is merged into it.
"""

from __future__ import annotations

import bisect
import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any, NamedTuple
from urllib.parse import quote, unquote

from lestash.core.activities import record_activities
from lestash.core.database import linkedin_person, mark_recent_history, max_history_id
from lestash.core.stubs import STUB_PREFIX, make_stub, merge_stub
from lestash.models.item import ActivityCreate

ORIGIN = "import:linkedin"
SOURCE = "linkedin"
FEED_URL = "https://www.linkedin.com/feed/update/"
# urn:li:comment:(activity:123,456) is comment 456 on post urn:li:activity:123
COMMENT_URN = re.compile(r"^urn:li:comment:\((\w+):([^,]+),([^)]+)\)$")
# The post a link names: urn:li:share:1, urn:li:ugcPost:1, urn:li:activity:1, or
# urn:li:groupPost:<group>-<id>
POST_URN = re.compile(r"urn:li:(?:share|ugcPost|activity|groupPost):[\d-]+")
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
    if not urn.startswith("urn:li:"):
        return None  # such as changelog-ugcPosts-<ms>, which is not a Snowflake id
    try:
        snowflake_id = int(urn.rsplit(":", 1)[-1].rsplit("-", 1)[-1])
    except ValueError:
        return None
    return snowflake_id >> 22


def _held_comments(conn: sqlite3.Connection) -> dict[str, int]:
    """The held LinkedIn comments, by comment id: the changelog's resourceId, the last
    part of a comment URN. The oldest wins an id held twice."""
    return {
        str(comment_id): item_id
        for item_id, comment_id in conn.execute(
            "SELECT id, json_extract(metadata, '$.raw.resourceId') FROM items "
            "WHERE source_type = 'linkedin' AND held = 1 "
            "AND json_extract(metadata, '$.resource_name') = 'socialActions/comments' "
            "AND json_extract(metadata, '$.raw.resourceId') IS NOT NULL ORDER BY id DESC"
        )
    }


def held_comment(comments: dict[str, int], urn: str) -> int | None:
    """The held comment a comment URN names, if LeStash holds it."""
    match = COMMENT_URN.match(urn)
    return comments.get(match.group(3)) if match else None


class HeldPosts:
    """The held LinkedIn posts, by their URNs and by when their URNs were made."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.by_urn: dict[str, int] = {}
        times: dict[int, set[int]] = {}
        # Posts from the sync and the export (ugcPosts), and from the snapshot fetch or
        # the import before #282, which name their post only in their link
        rows = conn.execute(
            "SELECT id, source_id, json_extract(metadata, '$.post_id'), url FROM items "
            "WHERE source_type = 'linkedin' AND held = 1 "
            "AND (json_extract(metadata, '$.resource_name') = 'ugcPosts' "
            "OR json_extract(metadata, '$.domain') = 'MEMBER_SHARE_INFO' "
            "OR json_extract(metadata, '$.raw_row') IS NOT NULL) ORDER BY id DESC"
        )
        # Newest first, so the oldest post is written last and wins a shared URN
        for item_id, source_id, post_id, url in rows:
            in_link = POST_URN.search(unquote(url or source_id or ""))
            for urn in (source_id, post_id, in_link.group(0) if in_link else None):
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


def _object(value: Any) -> dict[str, Any]:
    """value if it is a JSON object, else an empty one: one malformed row must not stop
    every sync."""
    return value if isinstance(value, dict) else {}


def _reaction_time(metadata: dict[str, Any]) -> datetime | None:
    """When a reaction was made, from LinkedIn's UTC ms: its own time if stored, else when
    the changelog captured it, within a second or so. Not created_at, which is local time:
    a run in another time zone would record the same like again."""
    raw = _object(metadata.get("raw"))
    created = _object(_object(raw.get("activity")).get("created"))
    for ms in (created.get("time"), raw.get("capturedAt"), raw.get("processedAt")):
        if isinstance(ms, int | float) and not isinstance(ms, bool):
            return datetime.fromtimestamp(ms / 1000, tz=UTC)
    return None


def _is_own_action(metadata: dict[str, Any]) -> bool:
    raw = _object(metadata.get("raw"))
    actor, owner = raw.get("actor"), raw.get("owner")
    return bool(actor) and actor == owner


_NOT_KNOWN = -1  # an actor who can't be named: no activity, as one without an actor is yours


def _actor(conn: sqlite3.Connection, metadata: dict[str, Any]) -> int | None:
    """Who made a reaction: None for you, else their person; _NOT_KNOWN if neither."""
    if _is_own_action(metadata):
        return None
    actor = _object(metadata.get("raw")).get("actor")
    if not isinstance(actor, str) or not actor.startswith(("urn:li:person:", "urn:li:company:")):
        return _NOT_KNOWN
    person_id = linkedin_person(conn, actor)
    if person_id is None:
        return _NOT_KNOWN
    row = conn.execute("SELECT is_self FROM people WHERE id = ?", (person_id,)).fetchone()
    return None if row and row[0] else person_id


def _is_created(metadata: dict[str, Any]) -> bool:
    """Whether the changelog event made the reaction, rather than changing or deleting it.
    Only the event that made it is a like; others would add likes that never happened."""
    return metadata.get("method", "CREATE") == "CREATE"


def _merge_filled_stubs(
    conn: sqlite3.Connection, held: HeldPosts, comments: dict[str, int]
) -> tuple[int, list[int]]:
    """Merge each stub whose post or comment is now held. Returns how many, and the ids
    of the children moved with them."""
    merged, moved = 0, []
    stubs = conn.execute(
        "SELECT id, json_extract(metadata, '$.post_urn'), "
        "json_extract(metadata, '$.comment_urn') FROM items "
        "WHERE source_type = 'linkedin' AND held = 0 AND source_id LIKE ?",
        (STUB_PREFIX + "%",),
    ).fetchall()
    for stub_id, urn, comment_urn in stubs:
        if comment_urn:
            found = held_comment(comments, comment_urn)
        else:
            found = held.find(urn) if urn else None
        if found is not None:
            moved += merge_stub(conn, stub_id, found)
            merged += 1
    return merged, moved


class _Pass:
    """One run of link_linkedin_targets: the held posts, and what it added."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.held = HeldPosts(conn)
        self.comments = _held_comments(conn)
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
        """The item a comment or reaction is on: a post, held or stub, or a comment,
        held or stub; a comment's stub replies to its post."""
        if not COMMENT_URN.match(urn):
            return self.post(urn)
        found = held_comment(self.comments, urn)
        if found is not None:
            return found
        stub = _comment_stub(self.conn, urn)
        post = post_urn(urn)
        self.link(stub, self.post(post), post)
        return stub


def link_linkedin_targets(conn: sqlite3.Connection) -> StubCounts:
    """Merge stubs whose post is now held, then link each comment and reaction to its
    target. Runs after resolve_linkedin_parents; safe to run again. The caller commits."""
    run = _Pass(conn)
    since = max_history_id(conn)
    merged, touched = _merge_filled_stubs(conn, run.held, run.comments)
    parented = activities = 0
    rows = conn.execute(
        "SELECT id, parent_id, metadata FROM items "
        "WHERE source_type = 'linkedin' AND held = 1 "
        "AND (json_extract(metadata, '$.commented_on') IS NOT NULL "
        "OR json_extract(metadata, '$.reacted_to') IS NOT NULL)"
    ).fetchall()
    for item_id, parent_id, metadata_json in rows:
        try:
            metadata = _object(json.loads(metadata_json))
        except (TypeError, ValueError):
            continue
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
        elif _is_created(metadata):
            when = _reaction_time(metadata)
            if when is None:
                continue
            actor_id = _actor(conn, metadata)
            if actor_id == _NOT_KNOWN:
                continue
            target_id = parent_id if parent_id is not None and not on_comment else None
            if (
                target_id is not None
                and not conn.execute("SELECT 1 FROM items WHERE id = ?", (target_id,)).fetchone()
            ):
                target_id = None  # parent_id has no foreign key: it can name a deleted item
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
                        actor_id=actor_id,
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
