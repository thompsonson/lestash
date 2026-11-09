"""Typed links between items, found by the linker pass after each sync.

See docs/content-and-activity-ontology-design.md, "Typed links". For now the linker
matches the web addresses in an item's content against the addresses of stored items:

- ``references``: A's content holds B's address.
- ``syndication``: B is a cross-posted copy of A. Your own top-level Bluesky or LinkedIn
  post, made within an hour of a Micro.blog post that lists that service in its
  ``syndication``, holding its address. A reply, comment, message or someone else's post
  holding the address only references it.

An address is an exact identifier, so these links are ``confirmed`` at once. Removing one
sets it ``rejected``; the row stays, so the linker never adds it again.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple
from urllib.parse import urlsplit, urlunsplit

ORIGIN = "linker:url"
URL = re.compile(r"https?://[^\s<>\"'()\[\]{}]+")
YOUTUBE_VIDEO = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)
# Characters that end a sentence or markup rather than an address
TRAILING = ".,;:!?*_`'\""
# Services a Micro.blog post can be cross-posted to, as source_type -> syndication name
SYNDICATED = {"bluesky": "bluesky", "linkedin": "linkedin"}
# A LinkedIn item is a post (not a comment, message or reaction) by its source_id
LINKEDIN_POSTS = ("changelog-ugcPosts-", "urn:li:share:", "urn:li:ugcPost:")
# A cross-post is made with its original; Micro.blog posts it within minutes
CROSS_POST_WINDOW = timedelta(hours=1)
# The start of the linked item's text that links_of returns, for one line in a list
PREVIEW_CHARS = 120


class Link(NamedTuple):
    from_id: int
    to_id: int
    kind: str
    evidence: str


def address_key(url: str) -> str | None:
    """The key two addresses of the same thing share, or None if it isn't an address.

    A YouTube video is its id, whatever the address form (watch, youtu.be, shorts). Other
    addresses drop the scheme, ``www.``, a trailing slash and the fragment.
    """
    url = url.rstrip(TRAILING)
    if match := YOUTUBE_VIDEO.search(url):
        return f"youtube:{match.group(1)}"
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = parts.netloc.lower().removeprefix("www.")
    if not host:
        return None
    return urlunsplit(("", host, parts.path.rstrip("/"), parts.query, ""))


class _Target(NamedTuple):
    id: int
    source_type: str
    metadata: Any
    created_at: datetime | None


# Sources that store created_at in this machine's local time, without a zone
LOCAL_TIME_SOURCES = frozenset({"linkedin"})


def aware_time(value: Any, source_type: str) -> datetime | None:
    """A stored created_at as an aware datetime, or None if it isn't a time.

    One with no zone is UTC, except from LinkedIn, whose extractor writes local time.
    """
    try:
        when = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if when.tzinfo:
        return when
    return when.astimezone() if source_type in LOCAL_TIME_SOURCES else when.replace(tzinfo=UTC)


def _targets(conn: sqlite3.Connection) -> dict[str, _Target]:
    """Each address key -> the item at that address.

    Of several items at one address, the canonical video item (``history:``) wins, then a
    held item over a stub, then a top-level item, then the oldest. A LinkedIn reaction or
    comment is never a target: it has the address of the post it is on.
    """
    rows = conn.execute(
        "SELECT id, source_type, url, metadata, created_at FROM items "
        "WHERE url IS NOT NULL AND url != '' "
        "AND NOT (source_type = 'linkedin' AND source_id LIKE 'changelog-socialActions/%') "
        "ORDER BY (source_id LIKE 'history:%') DESC, held DESC, (parent_id IS NULL) DESC, id"
    )
    targets: dict[str, _Target] = {}
    for item_id, source_type, url, metadata, created_at in rows:
        key = address_key(url)
        if key and key not in targets:
            targets[key] = _Target(
                item_id, source_type, metadata, aware_time(created_at, source_type)
            )
    return targets


def _is_reply(metadata: Any) -> bool:
    """Whether a post replies to another (Bluesky's metadata.reply_to), stored or not."""
    try:
        value = json.loads(metadata) if metadata else {}
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and bool(value.get("reply_to"))


def _is_cross_post(copy: sqlite3.Row | tuple[Any, ...], original: _Target) -> bool:
    """Whether an item holding a Micro.blog post's address is that post's cross-post: your
    own top-level post on a service the original lists, made within the window."""
    _, source_type, _, parent_id, created_at, source_id, is_own, metadata = copy
    service = SYNDICATED.get(source_type)
    if original.source_type != "microblog" or service not in _syndicated_to(original.metadata):
        return False
    if parent_id is not None or not is_own or _is_reply(metadata):
        return False
    if source_type == "linkedin" and not str(source_id).startswith(LINKEDIN_POSTS):
        return False
    when = aware_time(created_at, source_type)
    return (
        when is not None
        and original.created_at is not None
        and abs(when - original.created_at) <= CROSS_POST_WINDOW
    )


def _syndicated_to(metadata: Any) -> list[str]:
    try:
        value = json.loads(metadata) if metadata else {}
    except (TypeError, json.JSONDecodeError):
        return []
    services = value.get("syndication") if isinstance(value, dict) else None
    return [s for s in services if isinstance(s, str)] if isinstance(services, list) else []


def find_links(conn: sqlite3.Connection) -> Iterator[Link]:
    """Every link the addresses in stored content make."""
    targets = _targets(conn)
    rows = conn.execute(
        "SELECT id, source_type, content, parent_id, created_at, source_id, is_own_content, "
        "metadata FROM items WHERE instr(content, 'http') > 0"
    )
    for row in rows:
        item_id, content = row[0], row[2]
        seen: set[int] = set()
        for url in URL.findall(content):
            key = address_key(url)
            target = targets.get(key) if key else None
            if not target or target.id == item_id or target.id in seen:
                continue
            seen.add(target.id)
            evidence = url.rstrip(TRAILING)
            if _is_cross_post(row, target):
                # The Micro.blog post is the original; this post is its copy
                yield Link(target.id, item_id, "syndication", evidence)
            else:
                yield Link(item_id, target.id, "references", evidence)


def link_items(conn: sqlite3.Connection) -> int:
    """Store every link found that isn't stored yet, rejected ones included. Returns how
    many were added. The caller commits."""
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO item_links (from_id, to_id, kind, evidence, origin, status) "
        f"VALUES (?, ?, ?, ?, '{ORIGIN}', 'confirmed')",
        find_links(conn),
    )
    return conn.total_changes - before


def links_of(conn: sqlite3.Connection, item_id: int) -> list[dict[str, Any]]:
    """The confirmed links from and to an item, each with the item at its other end."""
    rows = conn.execute(
        """
        SELECT l.from_id = :id AS outgoing, l.kind, l.evidence, l.origin,
               i.id, i.source_type, i.title, i.url, substr(i.content, 1, :chars) AS preview
        FROM item_links l
        JOIN items i ON i.id = CASE WHEN l.from_id = :id THEN l.to_id ELSE l.from_id END
        WHERE (l.from_id = :id OR l.to_id = :id) AND l.status = 'confirmed'
        ORDER BY l.kind, outgoing DESC, i.id
        """,
        {"id": item_id, "chars": PREVIEW_CHARS},
    ).fetchall()
    return [
        {
            "direction": "outgoing" if row[0] else "incoming",
            "kind": row[1],
            "evidence": row[2],
            "origin": row[3],
            "item": {
                "id": row[4],
                "source_type": row[5],
                "title": row[6],
                "url": row[7],
                "preview": " ".join(row[8].split()) or None,
            },
        }
        for row in rows
    ]


def reject_link(conn: sqlite3.Connection, from_id: int, to_id: int, kind: str) -> bool:
    """Mark a link rejected, so it stops showing and the linker never adds it again.
    Returns False if there is no such link. The caller commits."""
    cursor = conn.execute(
        "UPDATE item_links SET status = 'rejected' WHERE from_id = ? AND to_id = ? AND kind = ?",
        (from_id, to_id, kind),
    )
    return cursor.rowcount > 0
