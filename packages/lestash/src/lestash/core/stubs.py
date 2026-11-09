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


def merge_stub(conn: sqlite3.Connection, stub_id: int, item_id: int) -> None:
    """Move a stub's links and activities onto the item it stood for, then delete it.

    A link or activity the item already has stays as it is; the stub's copy goes with
    the stub. A link between the two becomes a link to itself, and is dropped. The caller
    commits.
    """
    for column in ("from_id", "to_id"):
        conn.execute(
            f"UPDATE OR IGNORE item_links SET {column} = ? WHERE {column} = ?",
            (item_id, stub_id),
        )
    conn.execute("DELETE FROM item_links WHERE from_id = to_id AND from_id = ?", (item_id,))
    conn.execute(
        "UPDATE OR IGNORE activities SET item_id = ? WHERE item_id = ?", (item_id, stub_id)
    )
    conn.execute("UPDATE activities SET context_id = ? WHERE context_id = ?", (item_id, stub_id))
    conn.execute("DELETE FROM items WHERE id = ? AND held = 0", (stub_id,))
