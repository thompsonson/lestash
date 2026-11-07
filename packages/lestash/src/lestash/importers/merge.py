"""Fold a re-imported item into the copy already stored, instead of replacing it.

Upserts replace an item's content and metadata. That is wrong for two kinds of import:

- YouTube Takeout history (``history:``, ``search:``, ``post:``): each export holds only the
  watches YouTube still keeps (auto-delete drops old ones), so the watch times are merged.
  ``history:{video_id}`` is also written by the YouTube API sync; neither writer may wipe the
  other's fields (the API description, the Takeout watch times).
- NotebookLM notebooks from a multi-part Takeout: each part holds some of a notebook
  (metadata, sources, notes, artifacts), so the parts are combined. Only the part with the
  notebook's metadata JSON sets its title and date.
- NotebookLM notes, reports and chats: a child in a part without that JSON has only the
  folder name for its title prefix, and no date. It takes both from its stored notebook,
  and the notebook's children are corrected when the JSON arrives later.

Every writer that upserts sync or import items calls ``merge_with_stored`` first.
"""

import json
import sqlite3
from collections import Counter
from datetime import datetime
from typing import Any

from lestash.models.item import ItemCreate

MAX_TIMES = 200  # matches youtube_history.MAX_TIMES

# times list -> count key
TIME_KEYS = {
    "watched_times": "watched_count",
    "viewed_times": "viewed_count",
    "searched_times": "searched_count",
}
YOUTUBE_KINDS = ("history", "search", "post")
NOTEBOOK_CHILD_TYPES = ("note", "report", "chat")


def merge_with_stored(conn: sqlite3.Connection, item: ItemCreate) -> None:
    """Merge ``item`` with its stored copy, in place. No-op for other items or new items."""
    metadata = item.metadata or {}
    if not item.source_id:
        return
    if item.source_type == "youtube" and item.source_id.split(":", 1)[0] in YOUTUBE_KINDS:
        stored = _stored(conn, item)
        if stored:
            _merge_youtube(item, stored)
    elif item.source_type == "notebooklm" and metadata.get("type") == "notebook":
        stored = _stored(conn, item)
        if stored:
            _merge_notebook(conn, item, stored)
    elif item.source_type == "notebooklm" and metadata.get("type") in NOTEBOOK_CHILD_TYPES:
        _take_from_notebook(conn, item)


def _stored(conn: sqlite3.Connection, item: ItemCreate) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, title, content, metadata FROM items WHERE source_type = ? AND source_id = ?",
        (item.source_type, item.source_id),
    ).fetchone()
    if not row:
        return None
    try:
        metadata = json.loads(row[3]) if row[3] else {}
    except json.JSONDecodeError:
        metadata = {}
    return {"id": row[0], "title": row[1], "content": row[2], "metadata": metadata}


def _union_times(old: dict[str, Any], new: dict[str, Any], merged: dict[str, Any]) -> list[str]:
    """Union every times list into ``merged``; returns the (last) merged list."""
    result: list[str] = []
    for key, count_key in TIME_KEYS.items():
        if key not in old and key not in new:
            continue
        times = sorted({*old.get(key, []), *new.get(key, [])})
        merged[key] = times[-MAX_TIMES:]
        capped = len(old.get(key, [])) >= MAX_TIMES or len(new.get(key, [])) >= MAX_TIMES
        merged[count_key] = (
            max(len(times), old.get(count_key, 0), new.get(count_key, 0)) if capped else len(times)
        )
        if times:
            # The lists are capped, so an earlier first_at can live only in the stored copy
            firsts = [t for t in (times[0], old.get("first_at"), new.get("first_at")) if t]
            lasts = [t for t in (times[-1], old.get("last_at"), new.get("last_at")) if t]
            merged["first_at"], merged["last_at"] = min(firsts), max(lasts)
        result = times
    return result


def _merge_youtube(item: ItemCreate, stored: dict[str, Any]) -> None:
    from lestash.importers.youtube_history import search_content, video_content

    old, new = stored["metadata"], dict(item.metadata or {})
    new_is_takeout = new.get("source") == "takeout"
    old_is_takeout = old.get("source") == "takeout"
    merged = {**old, **new}
    times = [datetime.fromisoformat(t) for t in _union_times(old, new, merged)]

    kind = (item.source_id or "").split(":", 1)[0]
    if kind == "history":
        # The API sync stores the description as content; keep it in metadata either way.
        if not new_is_takeout and item.content:
            merged["description"] = item.content
        elif new_is_takeout and not old_is_takeout and stored["content"]:
            merged.setdefault("description", stored["content"])
        if (
            new_is_takeout
            and new.get("unavailable")
            and stored["title"]
            and not old.get("unavailable")
        ):
            # A video removed since: keep the real title we already had
            item.title = stored["title"]
            merged.pop("unavailable", None)
        if "watched_times" in merged:
            item.content = video_content(
                item.title or "", merged.get("channel_title"), times, merged.get("description")
            )
    elif kind == "search" and new_is_takeout:
        item.content = search_content(merged.get("query") or "", times)

    item.metadata = merged


def _union(old: list[Any] | None, new: list[Any] | None) -> list[Any]:
    """Order-preserving multiset union: two sources may share a title, so a value appears
    as often as it does in whichever list has more of it."""
    result = list(old or [])
    remaining = Counter(result)
    for value in new or []:
        if remaining[value]:
            remaining[value] -= 1
        else:
            result.append(value)
    return result


def _merge_notebook(conn: sqlite3.Connection, item: ItemCreate, stored: dict[str, Any]) -> None:
    from lestash.importers.zips import notebook_summary

    old, new = stored["metadata"], dict(item.metadata or {})
    # Notebooks imported before multi-part support came from a part with metadata.
    old_has_meta = old.get("has_metadata", True)
    new_has_meta = new.get("has_metadata", False)
    merged = {**old, **new}

    if old_has_meta and not new_has_meta:
        item.title = stored["title"]
        if "emoji" in old:
            merged["emoji"] = old["emoji"]
        merged["has_metadata"] = True
    elif new_has_meta and not old_has_meta:
        # The part with the metadata JSON arrived second: it has the real title and date.
        if item.created_at:
            conn.execute(
                "UPDATE items SET created_at = ? WHERE id = ?", (item.created_at, stored["id"])
            )
        _fix_notebook_children(conn, item.source_id or "", item.title or "", item.created_at)

    for key in ("sources", "artifacts", "notes", "chats"):
        values = _union(old.get(key), new.get(key))
        if values:
            merged[key] = sorted(values) if key == "artifacts" else values
    if merged.get("sources"):
        merged["source_count"] = len(merged["sources"])
    for key, count_key in (("notes", "note_count"), ("chats", "chat_count")):
        if key in old:
            merged[count_key] = len(merged.get(key, []))
        else:  # stored before names were kept
            merged[count_key] = max(len(merged.get(key, [])), old.get(count_key, 0))

    item.content = notebook_summary(item.title or "", merged)
    item.metadata = merged


def _child_title(notebook_title: str, child_source_id: str) -> str:
    """A child's title, as the parser builds it: the notebook title, then the file name."""
    name = child_source_id.rsplit("/", 1)[-1].removesuffix(".html").removesuffix(".md")
    return f"{notebook_title} — {name}"


def _take_from_notebook(conn: sqlite3.Connection, item: ItemCreate) -> None:
    """Give a NotebookLM child its notebook's title and date, once the notebook has them."""
    parent_source_id = (item.metadata or {}).get("_parent_source_id")
    if not parent_source_id or not item.source_id:
        return
    row = conn.execute(
        "SELECT title, created_at, metadata FROM items "
        "WHERE source_type = 'notebooklm' AND source_id = ?",
        (parent_source_id,),
    ).fetchone()
    if not row:
        return
    try:
        parent_metadata = json.loads(row[2]) if row[2] else {}
    except json.JSONDecodeError:
        parent_metadata = {}
    # Notebooks imported before multi-part support came from a part with metadata.
    if not parent_metadata.get("has_metadata", True):
        return
    item.title = _child_title(row[0] or "", item.source_id)
    if item.created_at is None and row[1]:
        # An insert takes this date; an upsert of a stored child keeps its old one, so set it.
        item.created_at = datetime.fromisoformat(str(row[1]))
        conn.execute(
            "UPDATE items SET created_at = ? WHERE source_type = 'notebooklm' AND source_id = ?",
            (row[1], item.source_id),
        )


def _fix_notebook_children(
    conn: sqlite3.Connection, notebook_source_id: str, title: str, created_at: datetime | None
) -> None:
    """Correct the title and date of children stored before their notebook's metadata."""
    from lestash.core.database import mark_recent_history, max_history_id

    # json_extract raises on malformed JSON, so such rows are skipped first
    rows = conn.execute(
        "SELECT id, source_id FROM items WHERE source_type = 'notebooklm' AND CASE "
        "WHEN json_valid(metadata) THEN json_extract(metadata, '$._parent_source_id') = ? "
        "AND json_extract(metadata, '$.type') IN ('note', 'report', 'chat') END",
        (notebook_source_id,),
    ).fetchall()
    since = max_history_id(conn)
    for child_id, child_source_id in rows:
        conn.execute(
            "UPDATE items SET title = ?, created_at = COALESCE(?, created_at) WHERE id = ?",
            (
                _child_title(title, child_source_id),
                created_at.isoformat(" ") if created_at else None,
                child_id,
            ),
        )
    mark_recent_history(conn, since, "sync", item_ids=[row[0] for row in rows])
