"""LinkedIn items held twice: the same changelog event under two source ids (#244).

Before #84 (April 2026) a changelog item's source id ended with the event's
processedAt; since then it ends with the resource id, so the same event imported again
made a second item. Where both copies hold the very same event (the same changelog
event id), the copy under the old id is merged into the current one: its links,
activities, children, tags, collections and media move, then it is deleted.

Copies of different events for one resource, such as edits to your profile or a
changed reaction, are kept: each records a change.
"""

from __future__ import annotations

import sqlite3
from typing import NamedTuple

from lestash.core.database import mark_recent_history, max_history_id
from lestash.core.stubs import merge_item


class Duplicate(NamedTuple):
    old_id: int  # under the processedAt source id: merged and deleted
    current_id: int  # under the resource id source id: kept
    resource_name: str


def find_duplicates(conn: sqlite3.Connection) -> list[Duplicate]:
    """Pairs of held LinkedIn items that are one changelog event under two source ids."""
    rows = conn.execute(
        """
        SELECT o.id, c.id, json_extract(o.metadata, '$.resource_name')
        FROM items o JOIN items c
          ON c.source_type = 'linkedin'
         AND c.source_id = 'changelog-' || json_extract(o.metadata, '$.resource_name')
             || '-' || json_extract(o.metadata, '$.raw.resourceId')
         AND c.id != o.id
        WHERE o.source_type = 'linkedin' AND o.held = 1 AND c.held = 1
          AND json_valid(o.metadata) AND json_valid(c.metadata)
          AND json_extract(o.metadata, '$.raw.id') IS NOT NULL
          AND json_extract(o.metadata, '$.raw.id') = json_extract(c.metadata, '$.raw.id')
        ORDER BY o.id
        """
    ).fetchall()
    return [Duplicate(int(old), int(current), str(name)) for old, current, name in rows]


def dedupe_linkedin(conn: sqlite3.Connection) -> list[Duplicate]:
    """Merge each old copy into its current one. Returns the pairs merged; safe to run
    again. The caller commits."""
    since = max_history_id(conn)
    pairs = find_duplicates(conn)
    moved: list[int] = []
    for pair in pairs:
        moved += merge_item(conn, pair.old_id, pair.current_id)
    mark_recent_history(conn, since, "linkedin dedupe", item_ids=moved)
    return pairs
