"""Items wanted on a device, and the copies LeStash put there (#338).

Only intent and bookkeeping are stored, as for collections (``collection_places``):

- **wanted**: you asked for an item on the device on its own. A collection shown there
  asks for each of its items too.
- **path, sha256, copied_at**: the copy LeStash made and checked. LeStash never
  overwrites or deletes a file on the device.

Whether a copy is on the device is worked out from the device's snapshots: its book is
held and linked ``alternate_of`` the item (``pdfs.link_copies``). Only the newest version
of a document is copied; older versions keep their notes in LeStash.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from lestash.core.blobs import media_file, sha_file
from lestash.core.collections import _GONE, _KOBO_BOOK, KOBO
from lestash.core.config import Config
from lestash.core.pdfs import versions

# The media that hold an item's PDF, best first
PDF_MEDIA = ("source_pdf", "pdf")


@dataclass(frozen=True)
class Wanted:
    """A file to put on the device."""

    item_id: int  # the version to copy: the newest
    asked_for: int  # the item you or a collection asked for
    title: str
    sha256: str
    file: Path


def set_wanted(conn: sqlite3.Connection, item_id: int, place: str, wanted: bool) -> None:
    """Ask for an item on a device on its own, or stop asking. The caller commits.

    Not asking any more never takes a copy off the device. Asking again forgets the
    copy made of its newest version, so one taken off the device is copied back.
    """
    was = conn.execute(
        "SELECT wanted FROM item_places WHERE item_id = ? AND place = ?", (item_id, place)
    ).fetchone()
    conn.execute(
        "INSERT INTO item_places (item_id, place, wanted) VALUES (?, ?, ?)"
        " ON CONFLICT (item_id, place) DO UPDATE SET wanted = excluded.wanted",
        (item_id, place, int(wanted)),
    )
    newest = versions(conn, item_id)[0]
    # Newly asked, or asked again for a copy taken off: forget the copy, so it is made
    # again. Not on every call: a copy still there would lose its record.
    if wanted and (not (was and was[0]) or taken_off(conn, newest, place)):
        conn.execute(
            "UPDATE item_places SET copied_at = NULL, removed_at = NULL, last_error = NULL"
            " WHERE item_id = ? AND place = ?",
            (newest, place),
        )


def record_removed(conn: sqlite3.Connection, item_id: int, place: str, when: str) -> None:
    """Record that a snapshot after the copy no longer held it. The caller commits."""
    conn.execute(
        "UPDATE item_places SET removed_at = ? WHERE item_id = ? AND place = ?",
        (when, item_id, place),
    )


def copies_made(conn: sqlite3.Connection, place: str) -> list[tuple[int, str, str]]:
    """Copies LeStash made and hasn't seen leave: (item id, path, copied_at)."""
    return [
        (r[0], r[1], r[2])
        for r in conn.execute(
            "SELECT item_id, path, copied_at FROM item_places WHERE place = ?"
            " AND copied_at IS NOT NULL AND path IS NOT NULL AND removed_at IS NULL",
            (place,),
        )
    ]


def taken_off(conn: sqlite3.Connection, item_id: int, place: str = KOBO) -> bool:
    """Whether the copy LeStash made of the item was on the Kobo and has left it: a
    snapshot after the copy no longer held it, or its Kobo book was marked gone (#314)
    at or after the time it was copied."""
    row = conn.execute(
        "SELECT copied_at, removed_at FROM item_places WHERE item_id = ? AND place = ?"
        " AND copied_at IS NOT NULL",
        (item_id, place),
    ).fetchone()
    if row is None:
        return False
    copied_at = _utc(row[0])
    if row[1] and _utc(row[1]) >= copied_at:
        return True  # gone before the Kobo imported it
    for (gone,) in conn.execute(
        f"""
        SELECT json_extract(i.metadata, '$.removed_from_device') FROM item_links l
        JOIN items i ON i.id = CASE WHEN l.from_id = :id THEN l.to_id ELSE l.from_id END
        WHERE l.kind = 'alternate_of' AND l.status = 'confirmed'
          AND :id IN (l.from_id, l.to_id) AND {_KOBO_BOOK} AND {_GONE}
        """,
        {"id": item_id},
    ):
        if _utc(gone) >= copied_at:
            return True
    return False


def _utc(text: object) -> datetime:
    when = datetime.fromisoformat(str(text).replace("Z", "+00:00").replace(" ", "T"))
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def marked(conn: sqlite3.Connection, place: str) -> set[int]:
    """Items you asked for on the device on their own."""
    return {
        r[0]
        for r in conn.execute(
            "SELECT item_id FROM item_places WHERE place = ? AND wanted = 1", (place,)
        )
    }


def asked_for(conn: sqlite3.Connection, place: str) -> set[int]:
    """Items wanted on the device: marked on their own, or in a collection shown there."""
    in_collections = {
        r[0]
        for r in conn.execute(
            "SELECT ci.item_id FROM collection_items ci"
            " JOIN collection_places cp ON cp.collection_id = ci.collection_id"
            " WHERE cp.place = ? AND cp.shown = 1",
            (place,),
        )
    }
    return marked(conn, place) | in_collections


def on_kobo(conn: sqlite3.Connection, item_id: int) -> bool:
    """Whether the item is a Kobo book still on the Kobo, or linked ``alternate_of`` one."""
    return bool(
        conn.execute(
            f"""
            SELECT 1 FROM items i WHERE i.id = :id AND {_KOBO_BOOK} AND NOT {_GONE}
            UNION ALL
            SELECT 1 FROM item_links l
            JOIN items i ON i.id = CASE WHEN l.from_id = :id THEN l.to_id ELSE l.from_id END
            WHERE l.kind = 'alternate_of' AND l.status = 'confirmed'
              AND :id IN (l.from_id, l.to_id) AND {_KOBO_BOOK} AND NOT {_GONE}
            LIMIT 1
            """,
            {"id": item_id},
        ).fetchone()
    )


def pdf_of(conn: sqlite3.Connection, config: Config, item_id: int) -> tuple[Path, str] | None:
    """The item's current PDF file and its SHA-256, or None if it holds none."""
    row = conn.execute(
        "SELECT sha256, local_path FROM item_media WHERE item_id = ?"
        " AND media_type IN (?, ?) AND superseded_at IS NULL"
        " ORDER BY media_type = ? DESC, id LIMIT 1",
        (item_id, *PDF_MEDIA, PDF_MEDIA[0]),
    ).fetchone()
    if row is None:
        return None
    path = media_file({"sha256": row[0], "local_path": row[1]}, config)
    if path is None:
        return None
    return path, row[0] or sha_file(path)


def to_copy(
    conn: sqlite3.Connection, config: Config, place: str = KOBO
) -> tuple[list[Wanted], list[str]]:
    """The files to put on the device, and why any item you marked can't go.

    Each item asked for goes as its newest version. Items already there, and items
    without a PDF (most collection members, such as videos), are left out; only an item
    you marked on its own is reported when it has no PDF.
    """
    wanted: dict[int, Wanted] = {}
    problems: list[str] = []
    mine = marked(conn, place)
    for item_id in sorted(asked_for(conn, place)):
        newest = versions(conn, item_id)[0]
        if newest in wanted or on_kobo(conn, newest):
            continue
        if taken_off(conn, newest, place):
            continue  # you took it off: it stays off until you send it again
        source_type, title = conn.execute(
            "SELECT source_type, title FROM items WHERE id = ?", (newest,)
        ).fetchone()
        if source_type == KOBO:
            continue  # a book taken off the Kobo stays off: LeStash doesn't put it back
        found = pdf_of(conn, config, newest)
        if found is None:
            if item_id in mine:
                problems.append(f"item {newest} has no PDF file to copy")
            continue
        wanted[newest] = Wanted(newest, item_id, title or f"item {newest}", found[1], found[0])
    return list(wanted.values()), problems


def record_copy(
    conn: sqlite3.Connection, item_id: int, place: str, path: str, sha256: str, when: str
) -> None:
    """Record a copy made and checked on the device. The caller commits."""
    conn.execute(
        "INSERT INTO item_places (item_id, place, path, sha256, copied_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (item_id, place) DO UPDATE SET path = excluded.path,"
        " sha256 = excluded.sha256, copied_at = excluded.copied_at, last_error = NULL",
        (item_id, place, path, sha256, when),
    )


def record_copy_error(conn: sqlite3.Connection, item_id: int, place: str, error: str) -> None:
    """Record why a copy failed. The caller commits."""
    conn.execute(
        "INSERT INTO item_places (item_id, place, last_error) VALUES (?, ?, ?)"
        " ON CONFLICT (item_id, place) DO UPDATE SET last_error = excluded.last_error",
        (item_id, place, error),
    )


@dataclass
class ItemPlace:
    """Where an item stands on a device, as the item page shows it."""

    marked: bool
    collections: list[str]  # shown collections there that hold it
    newest_id: int
    on: bool  # the newest version is on the device
    gone: bool = False  # its copy was on the device and was taken off
    path: str | None = None  # the copy LeStash made of the newest version
    copied_at: str | None = None
    error: str | None = None

    @property
    def wanted(self) -> bool:
        return self.marked or bool(self.collections)

    def text(self) -> str:
        """ "On the Kobo", "Copied, waiting for the Kobo to import it", "For the Kobo"."""
        if self.on:
            return "On the Kobo"
        if self.gone:
            return "Taken off the Kobo; send it again to copy it back"
        if self.error:
            return f"Not copied: {self.error}"
        if self.copied_at:
            return "Copied, waiting for the Kobo to import it"
        if self.wanted:
            return "For the Kobo, copied at the next plug-in"
        return "Not on the Kobo"


def item_place(conn: sqlite3.Connection, item_id: int, place: str = KOBO) -> ItemPlace:
    """Where an item stands on a device: asked for, copied, on it."""
    row = conn.execute(
        "SELECT wanted FROM item_places WHERE item_id = ? AND place = ?", (item_id, place)
    ).fetchone()
    collections = [
        r[0]
        for r in conn.execute(
            "SELECT c.name FROM collections c"
            " JOIN collection_items ci ON ci.collection_id = c.id"
            " JOIN collection_places cp ON cp.collection_id = c.id"
            " WHERE ci.item_id = ? AND cp.place = ? AND cp.shown = 1 ORDER BY c.name",
            (item_id, place),
        )
    ]
    newest = versions(conn, item_id)[0]
    copy = conn.execute(
        "SELECT path, copied_at, last_error FROM item_places WHERE item_id = ? AND place = ?",
        (newest, place),
    ).fetchone()
    return ItemPlace(
        marked=bool(row and row[0]),
        collections=collections,
        newest_id=newest,
        on=on_kobo(conn, newest),
        gone=taken_off(conn, newest, place),
        path=copy[0] if copy else None,
        copied_at=copy[1] if copy else None,
        error=copy[2] if copy else None,
    )
