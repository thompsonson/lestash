"""Typed links between items, found by the linker pass after each sync.

See docs/content-and-activity-ontology-design.md, "Typed links". For now the linker
matches the web addresses in an item's content against the addresses of stored items:

- ``references``: A's content holds B's address.
- ``syndication``: B is a cross-posted copy of A. A Bluesky or LinkedIn post holding the
  address of a Micro.blog post that lists that service in its ``syndication``.

An address is an exact identifier, so these links are ``confirmed`` at once. Removing one
sets it ``rejected``; the row stays, so the linker never adds it again.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
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


def _targets(conn: sqlite3.Connection) -> dict[str, tuple[int, str, Any]]:
    """Each address key -> (id, source_type, metadata) of the item at that address.

    Of several items at one address, the canonical video item (``history:``) wins, then a
    top-level item, then the oldest.
    """
    rows = conn.execute(
        "SELECT id, source_type, source_id, url, metadata, parent_id FROM items "
        "WHERE url IS NOT NULL AND url != '' "
        "ORDER BY (source_id LIKE 'history:%') DESC, (parent_id IS NULL) DESC, id"
    )
    targets: dict[str, tuple[int, str, Any]] = {}
    for item_id, source_type, _, url, metadata, _ in rows:
        key = address_key(url)
        if key and key not in targets:
            targets[key] = (item_id, source_type, metadata)
    return targets


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
        "SELECT id, source_type, content FROM items WHERE instr(content, 'http') > 0"
    )
    for item_id, source_type, content in rows:
        seen: set[int] = set()
        for url in URL.findall(content):
            key = address_key(url)
            target = targets.get(key) if key else None
            if not target or target[0] == item_id or target[0] in seen:
                continue
            seen.add(target[0])
            to_id, to_type, to_metadata = target
            evidence = url.rstrip(TRAILING)
            service = SYNDICATED.get(source_type)
            if to_type == "microblog" and service in _syndicated_to(to_metadata):
                # The Micro.blog post is the original; this post is its copy
                yield Link(to_id, item_id, "syndication", evidence)
            else:
                yield Link(item_id, to_id, "references", evidence)


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
               i.id, i.source_type, i.title, i.url
        FROM item_links l
        JOIN items i ON i.id = CASE WHEN l.from_id = :id THEN l.to_id ELSE l.from_id END
        WHERE (l.from_id = :id OR l.to_id = :id) AND l.status = 'confirmed'
        ORDER BY l.kind, outgoing DESC, i.id
        """,
        {"id": item_id},
    ).fetchall()
    return [
        {
            "direction": "outgoing" if row[0] else "incoming",
            "kind": row[1],
            "evidence": row[2],
            "origin": row[3],
            "item": {"id": row[4], "source_type": row[5], "title": row[6], "url": row[7]},
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
