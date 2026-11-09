"""Stub items: things LeStash knows of by their address, but does not hold.

See docs/content-and-activity-ontology-design.md, "Stub items for things not held".
A link or an activity needs an item at each end. When LeStash holds only one end, such
as your comment on a LinkedIn post it never imported, the other end is a stub:

- ``held = 0``, empty ``content``, and the address and identifier it is known by.
- Its ``source_id`` starts with ``stub:``, so no import ever writes over it.
- Default listings, search and embeddings skip it.
- When the thing itself is imported later, ``merge_stub`` moves the stub's links and
  activities onto the imported item and deletes the stub.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import suppress
from datetime import datetime
from typing import Any

STUB_PREFIX = "stub:"


def make_stub(
    conn: sqlite3.Connection,
    source_type: str,
    identifier: str,
    *,
    url: str | None = None,
    created_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    """The id of the stub for ``identifier``, made if there is none yet. The caller
    commits."""
    source_id = STUB_PREFIX + identifier
    conn.execute(
        "INSERT OR IGNORE INTO items (source_type, source_id, url, content, created_at, "
        "is_own_content, metadata, held) VALUES (?, ?, ?, '', ?, 0, ?, 0)",
        (
            source_type,
            source_id,
            url,
            created_at.isoformat() if created_at else None,
            json.dumps(metadata) if metadata else None,
        ),
    )
    row = conn.execute(
        "SELECT id FROM items WHERE source_type = ? AND source_id = ?", (source_type, source_id)
    ).fetchone()
    return int(row[0])


def merge_stub(conn: sqlite3.Connection, stub_id: int, item_id: int) -> list[int]:
    """Move a stub's links, activities, children, tags, collections and media onto the
    item it stood for, then delete it. See merge_item. The caller commits."""
    return merge_item(conn, stub_id, item_id, stub_only=True)


def merge_item(
    conn: sqlite3.Connection, from_id: int, item_id: int, *, stub_only: bool = False
) -> list[int]:
    """Move an item's links, activities, children, tags, collections and media onto
    another that stands for the same thing, then delete it and its embedding.

    A link, activity, tag or collection entry the item already has stays as it is; the
    merged copy's goes with it. A link between the two becomes a link to itself, and
    is dropped. Media move only to an item with none, so a copy's media are not added
    twice. With stub_only, only a stub (held = 0) is deleted. Returns the ids of the
    children moved, whose history the caller tags. The caller commits.
    """
    for column in ("from_id", "to_id"):
        conn.execute(
            f"UPDATE OR IGNORE item_links SET {column} = ? WHERE {column} = ?",
            (item_id, from_id),
        )
    conn.execute("DELETE FROM item_links WHERE from_id = to_id AND from_id = ?", (item_id,))
    conn.execute(
        "UPDATE OR IGNORE activities SET item_id = ? WHERE item_id = ?", (item_id, from_id)
    )
    conn.execute("UPDATE activities SET context_id = ? WHERE context_id = ?", (item_id, from_id))
    # Whatever was attached: notes and other children, tags, collections, media
    moved = [
        row[0]
        for row in conn.execute(
            "UPDATE items SET parent_id = ? WHERE parent_id = ? RETURNING id", (item_id, from_id)
        ).fetchall()
    ]
    for table in ("item_tags", "collection_items"):
        conn.execute(
            f"UPDATE OR IGNORE {table} SET item_id = ? WHERE item_id = ?", (item_id, from_id)
        )
    conn.execute(
        "UPDATE item_media SET item_id = :to WHERE item_id = :from "
        "AND NOT EXISTS (SELECT 1 FROM item_media WHERE item_id = :to)",
        {"to": item_id, "from": from_id},
    )
    deleted = conn.execute(
        "DELETE FROM items WHERE id = ?" + (" AND held = 0" if stub_only else ""), (from_id,)
    ).rowcount
    if deleted:
        # Its embedding goes too, or semantic search would return it. vec_items needs
        # sqlite-vec, which get_connection doesn't load. No table: nothing to remove
        with suppress(sqlite3.OperationalError, ImportError, AttributeError):
            try:
                conn.execute("DELETE FROM vec_items WHERE item_id = ?", (from_id,))
            except sqlite3.OperationalError:
                from lestash.core.embeddings import load_vec_extension

                load_vec_extension(conn)  # once per connection: loading again fails
                conn.execute("DELETE FROM vec_items WHERE item_id = ?", (from_id,))
    return moved
