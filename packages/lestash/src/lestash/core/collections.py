"""Collections: their reading order (#317), and the places they are shown (#315).

A Collection holds items. ``collection_items.position`` gives its reading order; items
without one come after, newest first. Where a collection is shown, such as on the Kobo,
is bookkeeping in ``collection_places``, not an object of its own: the Collection on the
Kobo is the same Collection (docs/ux-object-model-design.md, "Collections on the Kobo").

The Kobo itself is written by the Kobo plugin at each plug-in; this module says what
should be there, and how far the Kobo is from it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

KOBO = "kobo"

# A collection's items in reading order
ORDER_SQL = "ci.position IS NULL, ci.position, ci.added_at DESC, ci.item_id DESC"

# Kobo book items: everything from the Kobo but its highlights, notes and markups
_KOBO_BOOK = "i.source_type = 'kobo' AND i.source_id NOT LIKE 'bookmark:%'"
_GONE = "json_extract(i.metadata, '$.removed_from_device') IS NOT NULL"


def ordered_item_ids(conn: sqlite3.Connection, collection_id: int) -> list[int]:
    return [
        r[0]
        for r in conn.execute(
            f"SELECT ci.item_id FROM collection_items ci WHERE ci.collection_id = ?"
            f" ORDER BY {ORDER_SQL}",
            (collection_id,),
        )
    ]


def add_item(
    conn: sqlite3.Connection, collection_id: int, item_id: int, note: str | None = None
) -> bool:
    """Add an item at the end of the reading order. False if it was already there."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO collection_items (collection_id, item_id, note, position)"
        " VALUES (?, ?, ?, (SELECT COALESCE(MAX(position), 0) + 1 FROM collection_items"
        " WHERE collection_id = ?))",
        (collection_id, item_id, note, collection_id),
    )
    return cur.rowcount > 0


def move_item(conn: sqlite3.Connection, collection_id: int, item_id: int, position: int) -> bool:
    """Put an item at ``position`` (1 is first) and number the rest around it.

    False if the item is not in the collection. A position past the end puts it last.
    """
    ids = ordered_item_ids(conn, collection_id)
    if item_id not in ids:
        return False
    ids.remove(item_id)
    ids.insert(max(0, min(position - 1, len(ids))), item_id)
    conn.executemany(
        "UPDATE collection_items SET position = ? WHERE collection_id = ? AND item_id = ?",
        [(n, collection_id, i) for n, i in enumerate(ids, 1)],
    )
    return True


def set_shown(conn: sqlite3.Connection, collection_id: int, place: str, shown: bool) -> None:
    """Switch showing a collection in a place on or off; the next write there applies it."""
    conn.execute(
        "INSERT INTO collection_places (collection_id, place, shown) VALUES (?, ?, ?)"
        " ON CONFLICT (collection_id, place) DO UPDATE SET shown = excluded.shown",
        (collection_id, place, int(shown)),
    )


def kobo_books(conn: sqlite3.Connection, collection_id: int) -> dict[str, int]:
    """The Kobo books a collection holds, as Kobo ContentID → item id.

    An item that is a Kobo book counts as itself; another item counts through a
    confirmed ``alternate_of`` link to a Kobo book (its copy on the Kobo). Books marked
    gone from the Kobo (#314) are left out.
    """
    rows = conn.execute(
        f"""
        SELECT i.source_id, ci.item_id FROM collection_items ci
        JOIN items i ON i.id = ci.item_id
        WHERE ci.collection_id = :c AND {_KOBO_BOOK} AND NOT {_GONE}
        UNION
        SELECT i.source_id, ci.item_id FROM collection_items ci
        JOIN item_links l ON l.kind = 'alternate_of' AND l.status = 'confirmed'
            AND ci.item_id IN (l.from_id, l.to_id)
        JOIN items i ON i.id = CASE WHEN l.from_id = ci.item_id THEN l.to_id ELSE l.from_id END
        WHERE ci.collection_id = :c AND {_KOBO_BOOK} AND NOT {_GONE}
        """,
        {"c": collection_id},
    ).fetchall()
    books: dict[str, int] = {}
    for content_id, item_id in rows:
        books.setdefault(content_id, item_id)
    return books


@dataclass
class PlaceStatus:
    """How far a place is from what LeStash holds for a collection."""

    shown: bool
    on: int = 0  # written there and still held
    waiting: int = 0  # to add or remove at the next write
    missing: int = 0  # items with no copy there
    last_written: str | None = None
    error: str | None = None
    written: list[str] = field(default_factory=list)

    def text(self) -> str:
        """ "19 on the Kobo · 2 waiting · 2 not on the Kobo", leaving out zeros."""
        parts = [f"{self.on} on the Kobo"]
        if self.waiting:
            parts.append(f"{self.waiting} waiting")
        if self.missing:
            parts.append(f"{self.missing} not on the Kobo")
        return " · ".join(parts)


def kobo_status(conn: sqlite3.Connection, collection_id: int) -> PlaceStatus | None:
    """A collection's Kobo status, or None if it was never switched on."""
    row = conn.execute(
        "SELECT shown, written, last_written, last_error FROM collection_places"
        " WHERE collection_id = ? AND place = ?",
        (collection_id, KOBO),
    ).fetchone()
    if row is None:
        return None
    written = set(json.loads(row[1]) if row[1] else [])
    books = kobo_books(conn, collection_id)
    total = conn.execute(
        "SELECT COUNT(*) FROM collection_items WHERE collection_id = ?", (collection_id,)
    ).fetchone()[0]
    held = set(books) if row[0] else set()
    return PlaceStatus(
        shown=bool(row[0]),
        on=len(held & written),
        waiting=len(held ^ written),
        missing=total - len(set(books.values())),
        last_written=row[2],
        error=row[3],
        written=sorted(written),
    )


@dataclass
class Shelf:
    """What one collection should be on the Kobo: the plan for one place row."""

    place_id: int
    collection_id: int | None  # None: deleted in LeStash
    name: str | None
    shown: bool
    external_id: str | None
    external_name: str | None
    books: list[str]  # ContentIDs, in reading order


def kobo_shelves(conn: sqlite3.Connection) -> list[Shelf]:
    """Every collection the Kobo should show, or should stop showing."""
    rows = conn.execute(
        "SELECT p.id, p.collection_id, c.name, p.shown, p.external_id, p.external_name"
        " FROM collection_places p LEFT JOIN collections c ON c.id = p.collection_id"
        " WHERE p.place = ? ORDER BY p.id",
        (KOBO,),
    ).fetchall()
    shelves = []
    for place_id, cid, name, shown, ext_id, ext_name in rows:
        books: list[str] = []
        if cid is not None and shown:
            by_item: dict[int, list[str]] = {}
            for content_id, item_id in kobo_books(conn, cid).items():
                by_item.setdefault(item_id, []).append(content_id)
            for item_id in ordered_item_ids(conn, cid):
                books += sorted(by_item.get(item_id, []))
        shelves.append(
            Shelf(
                place_id=place_id,
                collection_id=cid,
                name=name,
                shown=bool(shown) and cid is not None,
                external_id=ext_id,
                external_name=ext_name,
                books=books,
            )
        )
    return shelves


def record_written(
    conn: sqlite3.Connection,
    shelf: Shelf,
    *,
    external_id: str | None,
    external_name: str | None,
    written: list[str],
    when: str | None,
) -> None:
    """Record what a write put in a place; ``when`` None keeps the last write's time
    (nothing changed). A shelf removed for a deleted collection drops its row; one
    switched off keeps it, with nothing written."""
    if shelf.collection_id is None and external_id is None:
        conn.execute("DELETE FROM collection_places WHERE id = ?", (shelf.place_id,))
        return
    conn.execute(
        "UPDATE collection_places SET external_id = ?, external_name = ?, written = ?,"
        " last_written = COALESCE(?, last_written), last_error = NULL WHERE id = ?",
        (
            external_id,
            external_name,
            json.dumps(written) if written else None,
            when,
            shelf.place_id,
        ),
    )


def record_error(conn: sqlite3.Connection, shelf: Shelf, error: str) -> None:
    conn.execute(
        "UPDATE collection_places SET last_error = ? WHERE id = ?", (error, shelf.place_id)
    )
