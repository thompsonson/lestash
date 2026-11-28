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
from datetime import datetime, timedelta
from typing import Any

from lestash.core.activities import record_activities, stored_times
from lestash.core.database import dump_metadata
from lestash.core.stubs import merge_item
from lestash.models.item import ActivityCreate

VIDEO = "history:"
COPIES = ("liked:", "shared:")
API_SOURCE = "youtube"  # activities.source of a like the API sync recorded
# Takeout and the API sync record one like up to a second apart (672 pairs, 18 Nov 2026)
LIKE_WINDOW = timedelta(minutes=1)


def known_like(conn: sqlite3.Connection, item_id: int, when: datetime, source: str) -> bool:
    """Whether item ``item_id`` has a like from another source within LIKE_WINDOW of
    ``when``: the same like, as another writer recorded it."""
    for (stored,) in conn.execute(
        "SELECT occurred_at FROM activities WHERE item_id = ? AND verb = 'liked' AND source != ?",
        (item_id, source),
    ):
        with suppress(ValueError, TypeError):
            other = datetime.fromisoformat(stored)
            if abs(other - when) <= LIKE_WINDOW:
                return True
    return False


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
    - A copy's ``liked_at`` becomes a ``liked`` activity, unless Takeout recorded it.
    - The kept item takes a copy's parent when it has none of its own.
    - A ``_parent_source_id`` naming a ``liked:`` or ``shared:`` item, which no longer
      exists, is removed from every item, or renamed to ``history:`` for a child.

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
        title, content = kept[3], kept[4]
        own = {r[0] for r in items}
        # Where the kept item goes, decided before the merges move anything: its own
        # place, else the first place a copy had. Each is the place above the video's
        # highest item in that chain, since the copies' parts become the kept item's.
        places = [_place(conn, kept_id, own)]
        places += [_place(conn, r[0], own) for r in items if r[0] != kept_id]
        if kept[2] is not None:
            conn.execute("UPDATE items SET parent_id = NULL WHERE id = ?", (kept_id,))
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
            merge_item(conn, copy[0], kept_id)
            folded += 1
        # Every copy is folded now, so this check sees where things finally stand: a
        # place now under the kept item would make a loop
        parent_id = next(
            (p for p in places if p is not None and not _within(conn, p, kept_id)), None
        )
        if parent_id is not None:
            conn.execute("UPDATE items SET parent_id = ? WHERE id = ?", (parent_id, kept_id))
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
            (VIDEO + video_id, title, content, dump_metadata(metadata), kept_id),
        )
        _record_like(conn, kept_id, liked_at)
    _drop_stale_markers(conn)
    return folded


def _place(conn: sqlite3.Connection, item_id: int, own: set[int]) -> int | None:
    """Where ``item_id`` sits, above the highest of the video's own items in its chain."""
    chain = _ancestors(conn, item_id)
    tops = [i for i, ancestor in enumerate(chain) if ancestor in own]
    rest = chain[tops[-1] + 1 :] if tops else chain
    return rest[0] if rest else None


def _ancestors(conn: sqlite3.Connection, item_id: int) -> list[int]:
    """The containers of ``item_id``, nearest first; stops at a loop."""
    chain: list[int] = []
    row = conn.execute("SELECT parent_id FROM items WHERE id = ?", (item_id,)).fetchone()
    current = row[0] if row else None
    while current is not None and current != item_id and current not in chain:
        chain.append(current)
        row = conn.execute("SELECT parent_id FROM items WHERE id = ?", (current,)).fetchone()
        current = row[0] if row else None
    return chain


def _within(conn: sqlite3.Connection, item_id: int, ancestor_id: int) -> bool:
    """Whether ``item_id`` is ``ancestor_id`` or sits somewhere under it."""
    seen: set[int] = set()
    current: int | None = item_id
    while current is not None and current not in seen:
        if current == ancestor_id:
            return True
        seen.add(current)
        row = conn.execute("SELECT parent_id FROM items WHERE id = ?", (current,)).fetchone()
        current = row[0] if row else None
    return False


def _record_like(conn: sqlite3.Connection, item_id: int, liked_at: list[str]) -> None:
    """A ``liked`` activity at the earliest ``liked_at``, unless Takeout recorded it."""
    times = []
    for value in liked_at:
        with suppress(ValueError, TypeError, AttributeError):
            times.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
    if times and not known_like(conn, item_id, min(times), API_SOURCE):
        like = ActivityCreate(verb="liked", occurred_at=min(times), source=API_SOURCE)
        record_activities(conn, item_id, [like])


def _drop_stale_markers(conn: sqlite3.Connection) -> None:
    """Takeout's videos named their ``liked:`` copy as parent; transcripts and comments
    did too. The video's marker goes; a child's names the ``history:`` item."""
    marker = "json_extract(metadata, '$._parent_source_id')"
    stale = (
        f"source_type = 'youtube' AND json_valid(metadata) "
        f"AND ({marker} GLOB 'liked:*' OR {marker} GLOB 'shared:*')"
    )
    conn.execute(
        f"UPDATE items SET metadata = json_remove(metadata, '$._parent_source_id') "
        f"WHERE source_id GLOB 'history:*' AND {stale}"
    )
    conn.execute(
        f"UPDATE items SET metadata = json_set(metadata, '$._parent_source_id', "
        f"'history:' || substr({marker}, instr({marker}, ':') + 1)) WHERE {stale}"
    )
