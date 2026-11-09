"""Store and read activities: one row per time someone did something to an item.

See docs/content-and-activity-ontology-design.md. Imports attach the times to the
items they write (``ItemCreate.activities``); each writer stores them with
record_activities() once the item has an id.
"""

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime

from lestash.models.item import ActivityCreate


def activity_time(when: datetime) -> str:
    """A time as ``activities.occurred_at`` holds it: UTC to the millisecond, such as
    2026-11-08T17:35:03.123Z. A time with no zone is taken as UTC."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    when = when.astimezone(UTC)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def record_activities(
    conn: sqlite3.Connection, item_id: int, activities: Iterable[ActivityCreate] | None
) -> int:
    """Store ``activities`` for item ``item_id``; one already stored is skipped.

    Returns how many were added.
    """
    rows = [
        (
            a.verb,
            item_id,
            activity_time(a.occurred_at),
            a.source,
            json.dumps(a.details) if a.details is not None else None,
            a.actor_id,
        )
        for a in activities or []
    ]
    if not rows:
        return 0
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO activities (verb, item_id, occurred_at, source, details, "
        "actor_id) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    return conn.total_changes - before


def stored_times(conn: sqlite3.Connection, item_id: int, verb: str) -> list[datetime]:
    """The times of your stored ``verb`` activities on item ``item_id``, oldest first."""
    rows = conn.execute(
        "SELECT DISTINCT occurred_at FROM activities WHERE item_id = ? AND verb = ? "
        "AND actor_id IS NULL ORDER BY occurred_at",
        (item_id, verb),
    ).fetchall()
    return [datetime.fromisoformat(row[0]) for row in rows]
