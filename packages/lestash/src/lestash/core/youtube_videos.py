"""One item per YouTube video (#368).

A video is the ``history:{video_id}`` item, however it arrived: a watch, a like or a
share. Before #368 the API sync's likes made a ``liked:`` item, with the Takeout
``history:`` item nested under it, and a share from the app made a ``shared:`` item.
Migration 36 folds each such copy into the video's one item, by ``fold_video_copies``.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import suppress
from datetime import datetime
from typing import Any

from lestash.core.activities import record_activities, stored_times
from lestash.core.stubs import merge_item
from lestash.models.item import ActivityCreate

VIDEO = "history:"
COPIES = ("liked:", "shared:")
API_SOURCE = "youtube"  # activities.source of a like the API sync recorded


def _metadata(text: str | None) -> dict[str, Any]:
    with suppress(TypeError, json.JSONDecodeError):
        value = json.loads(text) if text else {}
        return value if isinstance(value, dict) else {}
    return {}


def fold_video_copies(conn: sqlite3.Connection) -> int:
    """Fold every ``liked:`` and ``shared:`` item into its video's ``history:`` item.

    - The video's ``history:`` item is kept; else its oldest copy becomes it.
    - A copy's children, links, activities, tags, collections, people and media move to
      the kept item (``merge_item``), and the copy is deleted.
    - A copy's metadata fills keys the kept item lacks; the API description is kept as
      ``description`` and in the content.
    - A copy's ``liked_at`` becomes a ``liked`` activity, unless the item has a like.

    Returns how many items were folded away. The caller commits.
    """
    rows = conn.execute(
        "SELECT id, source_id, parent_id, title, content, author, metadata FROM items "
        "WHERE source_type = 'youtube' AND ("
        "source_id GLOB 'liked:*' OR source_id GLOB 'shared:*' OR source_id IN ("
        "SELECT 'history:' || substr(source_id, instr(source_id, ':') + 1) FROM items "
        "WHERE source_type = 'youtube' AND (source_id GLOB 'liked:*' OR source_id GLOB 'shared:*'))"
        ") ORDER BY id"
    ).fetchall()
    videos: dict[str, list[tuple[Any, ...]]] = {}
    for row in rows:
        videos.setdefault(row[1].split(":", 1)[1], []).append(row)

    folded = 0
    for video_id, items in videos.items():
        kept = next((r for r in items if r[1] == VIDEO + video_id), items[0])
        kept_id = kept[0]
        metadata = _metadata(kept[6])
        title, content, parent_id = kept[3], kept[4], kept[2]
        description = metadata.get("description")
        liked_at: list[str] = []
        for copy in items:
            if copy[0] == kept_id:
                continue
            copy_metadata = _metadata(copy[6])
            if copy_metadata.get("liked_at"):
                liked_at.append(copy_metadata["liked_at"])
            if not description and copy[4] and copy_metadata.get("source") != "takeout":
                description = copy[4]  # the API sync stored the description as content
            if metadata.get("unavailable") and copy[3] and not copy_metadata.get("unavailable"):
                # A video removed since Takeout saw it: keep the real title the copy had
                title = copy[3]
                metadata.pop("unavailable")
            for key, value in copy_metadata.items():
                metadata.setdefault(key, value)
            if parent_id == copy[0]:
                # The Takeout item was nested under the copy: it takes the copy's place
                parent_id = copy[2]
                conn.execute("UPDATE items SET parent_id = ? WHERE id = ?", (parent_id, kept_id))
            merge_item(conn, copy[0], kept_id)
            folded += 1
        if metadata.get("liked_at"):
            liked_at.append(metadata["liked_at"])

        metadata["source_subtype"] = "history"
        if str(metadata.get("_parent_source_id", "")).startswith(COPIES):
            metadata.pop("_parent_source_id")
        if metadata.get("source") == "takeout" and (description or title != kept[3]):
            from lestash.importers.youtube_history import video_content

            if description:
                metadata["description"] = description
            times = stored_times(conn, kept_id, "watched")
            content = video_content(title or "", metadata.get("channel_title"), times, description)
        conn.execute(
            "UPDATE items SET source_id = ?, title = ?, content = ?, metadata = ? WHERE id = ?",
            (VIDEO + video_id, title, content, json.dumps(metadata), kept_id),
        )
        _record_like(conn, kept_id, liked_at)
    return folded


def _record_like(conn: sqlite3.Connection, item_id: int, liked_at: list[str]) -> None:
    """A ``liked`` activity at the earliest ``liked_at``, unless the item has a like."""
    if conn.execute(
        "SELECT 1 FROM activities WHERE item_id = ? AND verb = 'liked'", (item_id,)
    ).fetchone():
        return
    times = []
    for value in liked_at:
        with suppress(ValueError, TypeError, AttributeError):
            times.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
    if times:
        like = ActivityCreate(verb="liked", occurred_at=min(times), source=API_SOURCE)
        record_activities(conn, item_id, [like])
