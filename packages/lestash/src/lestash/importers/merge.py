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
- Gemini chats from Takeout activity: a chat already saved from the Gemini page or by the
  extension (matched by its exact chat id) keeps its item, title and turns. Takeout adds
  only the prompts that chat lacks, and never replaces a turn. A chat's prompt times are
  merged across exports.

The times themselves are activities (``ItemCreate.activities``), stored apart from
the item; the merge reads the stored ones and sums them all up in the metadata's count,
first_at and last_at.

Every writer that upserts sync or import items calls ``merge_with_stored`` first, and
skips the item when it returns False. It then stores the item's activities with
``record_activities``.
"""

import json
import sqlite3
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, NamedTuple

from lestash.core.activities import activity_time, stored_times
from lestash.importers.gemini_activity import SOURCE as GEMINI_ACTIVITY_SOURCE
from lestash.importers.gemini_activity import chat_content, prompt_key
from lestash.models.item import ItemCreate

# verb -> count key in metadata
COUNT_KEYS = {
    "watched": "watched_count",
    "viewed": "viewed_count",
    "searched": "searched_count",
    "prompted": "prompted_count",
}
YOUTUBE_KINDS = ("history", "search", "post")
NOTEBOOK_CHILD_TYPES = ("note", "report", "chat")


def merge_with_stored(conn: sqlite3.Connection, item: ItemCreate) -> bool:
    """Merge ``item`` with its stored copy, in place. No-op for other items or new items.

    Returns False when the item must not be stored, because a stored item already holds it.
    """
    metadata = item.metadata or {}
    if not item.source_id:
        return True
    if item.source_type == "gemini" and metadata.get("source") == GEMINI_ACTIVITY_SOURCE:
        return _merge_gemini(conn, item)
    if item.source_type == "youtube" and item.source_id.split(":", 1)[0] in YOUTUBE_KINDS:
        stored = _stored(conn, item)
        if stored:
            _merge_youtube(conn, item, stored)
    elif item.source_type == "notebooklm" and metadata.get("type") == "notebook":
        stored = _stored(conn, item)
        if stored:
            _merge_notebook(conn, item, stored)
    elif item.source_type == "notebooklm" and metadata.get("type") in NOTEBOOK_CHILD_TYPES:
        _take_from_notebook(conn, item)
    return True


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


def _union_times(
    conn: sqlite3.Connection, item: ItemCreate, stored: dict[str, Any], merged: dict[str, Any]
) -> list[datetime]:
    """Sum up the stored times and the item's new ones in ``merged``: count, first, last.

    The times themselves are activities: the stored ones are read from the table, and
    the item's are stored by its writer. Returns every time of the (last) verb, in order.
    """
    old, new = stored["metadata"], item.metadata or {}
    result: list[datetime] = []
    for verb, count_key in COUNT_KEYS.items():
        incoming = [a.occurred_at for a in item.activities or [] if a.verb == verb]
        if count_key not in old and count_key not in new and not incoming:
            continue
        times = sorted(
            {
                activity_time(t): t for t in (*stored_times(conn, stored["id"], verb), *incoming)
            }.items()
        )
        # A count can exceed the times stored: lists were capped at 200 before the table
        merged[count_key] = max(len(times), old.get(count_key, 0), new.get(count_key, 0))
        if times:
            # A capped list could drop an earlier first_at, kept only in the stored copy.
            # On a tie the item's own string wins, so a re-import leaves it unchanged.
            first, last = times[0][1].isoformat(), times[-1][1].isoformat()
            merged["first_at"] = _pick(min, new.get("first_at"), old.get("first_at"), first)
            merged["last_at"] = _pick(max, new.get("last_at"), old.get("last_at"), last)
        result = [t for _, t in times]
    return result


def _pick(choose: Callable[..., Any], *values: Any) -> str:
    """The earliest (``min``) or latest (``max``) of the values that are ISO times.

    A time with no zone is taken as UTC. Of equal times the first given is kept, as
    written. The last value must be a time.
    """
    times = []
    for value in values:
        try:
            when = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            continue
        times.append((when if when.tzinfo else when.replace(tzinfo=UTC), value))
    return str(choose(times, key=lambda t: t[0])[1])


def _merge_youtube(conn: sqlite3.Connection, item: ItemCreate, stored: dict[str, Any]) -> None:
    from lestash.importers.youtube_history import search_content, video_content

    old, new = stored["metadata"], dict(item.metadata or {})
    new_is_takeout = new.get("source") == "takeout"
    old_is_takeout = old.get("source") == "takeout"
    merged = {**old, **new}
    times = _union_times(conn, item, stored, merged)

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
        if "watched_count" in merged:
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
    from lestash.importers.zips import notebook_child_name

    return f"{notebook_title} — {notebook_child_name(child_source_id)}"


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
        try:
            item.created_at = datetime.fromisoformat(str(row[1]))
        except ValueError:
            return
        # An insert takes this date; an upsert of a stored child keeps its old one, so set it.
        conn.execute(
            "UPDATE items SET created_at = ? WHERE source_type = 'notebooklm' AND source_id = ?",
            (row[1], item.source_id),
        )


def _fix_notebook_children(
    conn: sqlite3.Connection, notebook_source_id: str, title: str, created_at: datetime | None
) -> None:
    """Correct the title and date of children stored before their notebook's metadata.

    Only titles still made from the folder name change, so a title edited by hand stays.
    """
    from lestash.core.database import mark_recent_history, max_history_id

    # json_extract raises on malformed JSON, so such rows are skipped first
    folder_prefix = f"{notebook_source_id.rsplit('/', 1)[-1]} — "
    rows = conn.execute(
        "SELECT id, source_id, title FROM items WHERE source_type = 'notebooklm' AND CASE "
        "WHEN json_valid(metadata) THEN json_extract(metadata, '$._parent_source_id') = ? "
        "AND json_extract(metadata, '$.type') IN ('note', 'report', 'chat') END",
        (notebook_source_id,),
    ).fetchall()
    since = max_history_id(conn)
    for child_id, child_source_id, child_title in rows:
        if (child_title or "").startswith(folder_prefix):
            child_title = _child_title(title, child_source_id)
        conn.execute(
            "UPDATE items SET title = ?, created_at = COALESCE(?, created_at) WHERE id = ?",
            (child_title, created_at.isoformat(" ") if created_at else None, child_id),
        )
    mark_recent_history(conn, since, "sync", item_ids=[row[0] for row in rows])


class _GeminiChat(NamedTuple):
    id: int
    source_id: str
    metadata: dict[str, Any]

    @property
    def from_takeout(self) -> bool:
        return self.metadata.get("source") == GEMINI_ACTIVITY_SOURCE


def _gemini_chat(conn: sqlite3.Connection, conversation_id: str) -> _GeminiChat | None:
    """The stored chat with this exact id.

    Ids match exactly: the extension's ``source_id`` (``c_<id>``), a saved page's URL
    (also with ``/u/<n>/``, a trailing slash or a query), or a ``metadata.conversation_id``.
    One stored as ``c_<id>`` wins, whoever wrote it; then page saves, the one with turns
    first, then the oldest.
    """
    # The usual case, by the (source_type, source_id) index: Takeout's own chat
    row = conn.execute(
        "SELECT id, source_id, metadata FROM items WHERE source_type = 'gemini' AND source_id = ?",
        (conversation_id,),
    ).fetchone()
    if not row:
        chat_id = conversation_id.removeprefix("c_")
        app_url = f"https://gemini.google.com/*app/{chat_id}"
        # instr() cheaply skips rows that can't hold the id; json_extract raises on
        # malformed JSON, so such rows are skipped too
        row = conn.execute(
            "SELECT p.id, p.source_id, p.metadata FROM items p "
            "WHERE p.source_type = 'gemini' AND ("
            "p.source_id GLOB ? OR p.source_id GLOB ? OR CASE WHEN instr(p.metadata, ?) "
            "AND json_valid(p.metadata) "
            "THEN json_extract(p.metadata, '$.conversation_id') = ? END) "
            "ORDER BY EXISTS (SELECT 1 FROM items c WHERE c.parent_id = p.id "
            "AND c.source_type = 'gemini') DESC, p.id "
            "LIMIT 1",
            (app_url, f"{app_url}[/?#]*", conversation_id, conversation_id),
        ).fetchone()
    if not row:
        return None
    try:
        metadata = json.loads(row[2]) if row[2] else {}
    except json.JSONDecodeError:
        metadata = {}
    return _GeminiChat(row[0], row[1], metadata if isinstance(metadata, dict) else {})


def _merge_gemini(conn: sqlite3.Connection, item: ItemCreate) -> bool:
    metadata = dict(item.metadata or {})
    prompt = metadata.pop("_prompt", None)
    occurrence = int(metadata.pop("_occurrence", 1))
    item.metadata = metadata
    if metadata.get("type") == "chat":
        chat = _gemini_chat(conn, item.source_id or "")
        if chat is None:
            return True
        if not chat.from_takeout:
            return False  # saved from the Gemini page: that item stays as it is
        stored = _stored(conn, item)
        if stored:
            _merge_gemini_chat(conn, item, stored)
        return True

    if metadata.get("type") == "canvas":
        # An export without the document must not blank the stored copy
        return bool(metadata.get("content_in_export", True)) or not _stored(conn, item)

    parent_source_id = metadata.get("_parent_source_id")
    if not parent_source_id:
        return True
    chat = _gemini_chat(conn, parent_source_id)
    if chat is None or chat.from_takeout:
        return True
    # A turn of a chat saved from the Gemini page
    item.parent_id = chat.id
    metadata["_parent_source_id"] = chat.source_id
    if not prompt_key(prompt if item.author == "model" else item.content):
        # A prompt with no text (an attachment only) can't be matched to the page's turns,
        # and adding it could repeat one the page has: leave the page chat as it is
        return False
    turns = _page_turns(conn, chat)
    if item.author != "model":
        return _page_prompt(turns, item.content, occurrence) is None
    # A reply: keep the page's when it has one right after the same prompt
    index = _page_prompt(turns, prompt, occurrence)
    if index is None:
        return True
    if index + 1 < len(turns) and not turns[index + 1].is_user:
        return False
    # The prompt is the page's, so point at that turn instead of Takeout's
    if turns[index].source_id:
        metadata["prompt_source_id"] = turns[index].source_id
    else:
        metadata.pop("prompt_source_id", None)
    return True


class _PageTurn(NamedTuple):
    is_user: bool
    text: str
    source_id: str | None  # None for a turn kept in the extension's metadata


def _page_turns(conn: sqlite3.Connection, chat: _GeminiChat) -> list[_PageTurn]:
    """A page chat's turns in order.

    The chat's child turns, not counting those Takeout added; or, for a chat saved by the
    extension, the turns it keeps in metadata.
    """
    rows = conn.execute(
        "SELECT author, content, source_id FROM items WHERE parent_id = ? "
        "AND source_type = 'gemini' AND COALESCE("
        "CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.source') END, '') != ? "
        "ORDER BY id",
        (chat.id, GEMINI_ACTIVITY_SOURCE),
    ).fetchall()
    if rows:
        return [_PageTurn(author == "user", content or "", sid) for author, content, sid in rows]
    turns = chat.metadata.get("turns")
    if not isinstance(turns, list):
        return []
    return [
        _PageTurn(t.get("role") == "user", str(t.get("text") or ""), None)
        for t in turns
        if isinstance(t, dict)
    ]


def _page_prompt(turns: list[_PageTurn], prompt: str | None, occurrence: int) -> int | None:
    """Where the page has the ``occurrence``-th prompt with this text, matched by
    prompt_key(), if it has.

    ``occurrence`` counts within one export, which holds only the prompts Gemini still
    keeps, so after auto-delete a repeated short prompt can match an older one.
    """
    key = prompt_key(prompt)
    if not key:
        return None
    seen = 0
    for index, turn in enumerate(turns):
        if turn.is_user and prompt_key(turn.text) == key:
            seen += 1
            if seen == occurrence:
                return index
    return None


def _merge_gemini_chat(conn: sqlite3.Connection, item: ItemCreate, stored: dict[str, Any]) -> None:
    """Keep the prompt times of every export: each holds only what Gemini still keeps."""
    old, new = stored["metadata"], dict(item.metadata or {})
    merged = {**old, **new}
    _union_times(conn, item, stored, merged)
    notebooks = _union(old.get("notebooklm_notebooks"), new.get("notebooklm_notebooks"))
    if notebooks:
        merged["notebooklm_notebooks"] = notebooks
    first_new, first_old = new.get("first_at"), old.get("first_at")
    earlier = bool(first_new) and (not first_old or first_new < first_old)
    generated = old.get("generated_title", stored["title"])
    merged["generated_title"] = new.get("generated_title") if earlier else generated
    if stored["title"] != generated or not earlier:
        # A title you gave stays; so does one from a first prompt this export lacks
        item.title = stored["title"]
    if earlier and first_old and first_new:
        # An older export: the upsert keeps created_at, so date the chat from it here
        conn.execute(
            "UPDATE items SET created_at = ? WHERE id = ?",
            (datetime.fromisoformat(first_new).isoformat(" "), stored["id"]),
        )
    item.content = chat_content(merged.get("prompted_count", 0))
    item.metadata = merged
