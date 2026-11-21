"""Merge saved links to arXiv papers into the papers' arXiv items (#398).

Before #395 a shared arXiv link was saved as a plain link (a ``share``, or a ``web``
page). Each one moves into its paper's arXiv item: your note, the shared text and
title, when you shared it, its links, collections, tags, places and children. Then the
link is deleted; the paper keeps a record of it under ``merged_from``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from lestash_arxiv.client import Paper, get_paper
from lestash_arxiv.share import _add_text, _metadata, arxiv_id_from_url, import_paper
from lestash_arxiv.source import SOURCE_TYPE

# Rows of an item that merge_item leaves; a row the paper already has stays its own
_MOVED = ("item_places", "syndications")


@dataclass(frozen=True)
class Link:
    item_id: int
    source_type: str
    arxiv_id: str
    title: str | None


@dataclass(frozen=True)
class Merged:
    link_id: int
    paper_id: int
    created: bool  # the paper's arXiv item was made for this merge


def saved_links(conn: sqlite3.Connection) -> list[Link]:
    """Items, other than arXiv items, whose address is an arXiv paper's."""
    rows = conn.execute(
        "SELECT id, source_type, url, title FROM items "
        "WHERE source_type != ? AND url LIKE '%arxiv.org/%' ORDER BY id",
        (SOURCE_TYPE,),
    ).fetchall()
    found = []
    for item_id, source_type, url, title in rows:
        arxiv_id = arxiv_id_from_url(url or "")
        if arxiv_id:
            found.append(Link(item_id, source_type, arxiv_id, title))
    return found


def _shared_text(content: str | None, notes: str | None) -> str | None:
    """The text that came with a share, without the note the capture put first."""
    text = (content or "").strip()
    if notes and text.startswith(notes.strip()):
        text = text[len(notes.strip()) :].strip()
        text = text.removeprefix("---").strip()
    return text or None


class MergeError(ValueError):
    """A link that can't be merged: gone, or the paper's own item."""


def _under(conn: sqlite3.Connection, item_id: int, ancestor: int) -> bool:
    """True if ``item_id`` is ``ancestor`` or sits somewhere below it."""
    seen: set[int] = set()
    current: int | None = item_id
    while current is not None and current not in seen:
        if current == ancestor:
            return True
        seen.add(current)
        row = conn.execute("SELECT parent_id FROM items WHERE id = ?", (current,)).fetchone()
        current = row[0] if row else None
    return False


def merge_link(
    conn: sqlite3.Connection,
    link: Link,
    *,
    fetch: Callable[[str], Paper | None] = get_paper,
) -> Merged | None:
    """Move one saved link into its paper's arXiv item, then delete it. None if arXiv
    has no such paper; MergeError if the link is gone. Commits."""
    from lestash.core.pdf_enrich.persistence import SOURCE_PDF_MEDIA_TYPE
    from lestash.core.stubs import merge_item

    row = conn.execute(
        "SELECT source_type, url, title, content, metadata, created_at, parent_id "
        "FROM items WHERE id = ?",
        (link.item_id,),
    ).fetchone()
    if row is None:
        raise MergeError(f"item {link.item_id} no longer exists")
    source_type, url, title, content, raw_meta, created_at, parent_id = row
    meta = _metadata(conn, link.item_id)
    notes = meta.get("notes") if isinstance(meta.get("notes"), str) else None
    shared_text = meta.get("shared_text")
    if not shared_text and source_type == "share":
        # A share's text is what was shared; a saved page's is the page, not kept here
        shared_text = _shared_text(content, notes)
    if shared_text and shared_text.strip() == (url or "").strip():
        shared_text = None

    # The share's own saved activity moves below, so the paper isn't saved "now" as well
    imported = import_paper(conn, link.arxiv_id, record_share=False, fetch=fetch)
    if imported is None:
        return None
    paper_id = imported.item_id
    if paper_id == link.item_id:
        raise MergeError(f"item {link.item_id} is the paper's own item")

    for key, text in (("notes", notes), ("shared_text", shared_text)):
        if text:
            _add_text(conn, paper_id, key, text)
    if title and title != imported.title:
        _add_text(conn, paper_id, "shared_title", title)

    # The paper takes the link's place under its parent, if it has none of its own and
    # that wouldn't put it under itself; a paper filed under the link moves up with it
    paper_parent = conn.execute("SELECT parent_id FROM items WHERE id = ?", (paper_id,)).fetchone()[
        0
    ]
    if paper_parent == link.item_id:
        paper_parent = None
        conn.execute("UPDATE items SET parent_id = NULL WHERE id = ?", (paper_id,))
    if paper_parent is None and parent_id is not None and not _under(conn, parent_id, paper_id):
        conn.execute("UPDATE items SET parent_id = ? WHERE id = ?", (parent_id, paper_id))

    # What merge_item doesn't carry: places and syndications, and media. The paper may
    # hold its PDF address already, so only a source PDF it lacks comes across
    for table in _MOVED:
        conn.execute(
            f"UPDATE OR IGNORE {table} SET item_id = ? WHERE item_id = ?", (paper_id, link.item_id)
        )
    conn.execute(
        "UPDATE item_media SET item_id = :to WHERE item_id = :from AND (media_type != :pdf "
        "OR NOT EXISTS (SELECT 1 FROM item_media WHERE item_id = :to AND media_type = :pdf))",
        {"to": paper_id, "from": link.item_id, "pdf": SOURCE_PDF_MEDIA_TYPE},
    )

    record = {
        "item_id": link.item_id,
        "source_type": source_type,
        "url": url,
        "title": title,
        "created_at": str(created_at) if created_at else None,
        "metadata": json.loads(raw_meta) if raw_meta else None,
    }
    paper_meta = _metadata(conn, paper_id)
    merged = [m for m in paper_meta.get("merged_from", []) if m.get("item_id") != link.item_id]
    paper_meta["merged_from"] = [*merged, record]
    conn.execute("UPDATE items SET metadata = ? WHERE id = ?", (json.dumps(paper_meta), paper_id))
    # Links, activities, children, tags, collections, people, mentions; then the link
    # and its embedding are deleted
    merge_item(conn, link.item_id, paper_id)
    conn.commit()
    return Merged(link.item_id, paper_id, imported.created)
