"""Write the collections switched on in LeStash to the Kobo, at a plug-in (K8, #315).

Collections go one way, from LeStash to the Kobo. The Kobo keeps them in
``KoboReader.sqlite``: a ``Shelf`` row each, and a ``ShelfContent`` row per book, keyed
by the shelf's name. Collections made on the Kobo are never read or changed.

The write is planned on a copy of the newest snapshot's database, and only touches the
Kobo when something changes. On the Kobo it first checks the database is the one in the
snapshot, so the snapshot is its backup; after writing it runs ``integrity_check``, and
puts the snapshot's copy back if that fails.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from lestash.core.collections import Shelf

from lestash_kobo.store import DB_PATH, KoboStore, Snapshot

WAL = DB_PATH + "-wal"
SHM = DB_PATH + "-shm"


class KoboChanged(RuntimeError):
    """The Kobo's database is not the one in the newest snapshot."""


class KoboDamaged(RuntimeError):
    """The Kobo's database failed its check after the write, and was put back."""


@dataclass
class ShelfResult:
    """What one collection's write did, or would do."""

    shelf: Shelf
    external_id: str | None = None
    external_name: str | None = None
    written: list[str] = field(default_factory=list)  # ContentIDs now on the shelf
    added: int = 0
    removed: int = 0
    not_on_kobo: int = 0  # books LeStash holds that the Kobo doesn't (yet)
    error: str | None = None


def now_text() -> str:
    """A time as the Kobo writes it: ISO, UTC, ``Z``, to the second."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_sha(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _forget(conn: sqlite3.Connection, shelf_id: str, name: str | None, when: str) -> int:
    """Mark a shelf LeStash wrote, and its books, deleted, as the Kobo does."""
    n = conn.execute(
        "UPDATE Shelf SET _IsDeleted = 'true', _IsSynced = 'false', LastModified = ?"
        " WHERE Id = ? AND _IsDeleted != 'true'",
        (when, shelf_id),
    ).rowcount
    if name:
        n += conn.execute(
            "UPDATE ShelfContent SET _IsDeleted = 'true', _IsSynced = 'false',"
            " DateModified = ? WHERE ShelfName = ? AND _IsDeleted != 'true'",
            (when, name),
        ).rowcount
    return n


def apply(
    conn: sqlite3.Connection,
    shelves: list[Shelf],
    when: str,
    new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> list[ShelfResult]:
    """Make the Kobo database ``conn`` show ``shelves``; the caller commits.

    Deterministic for a given ``when`` and ``new_id``, so a plan made on a copy is the
    same write on the Kobo.
    """
    books_here = {
        r[0] for r in conn.execute("SELECT ContentID FROM content WHERE ContentType = '6'")
    }
    results = []
    for shelf in shelves:
        res = ShelfResult(shelf, external_id=shelf.external_id, external_name=shelf.external_name)
        results.append(res)
        if not shelf.shown or (shelf.external_name and shelf.external_name != shelf.name):
            # Switched off, deleted in LeStash, or renamed: take the old shelf away
            if shelf.external_id:
                res.removed = _forget(conn, shelf.external_id, shelf.external_name, when)
            res.external_id = res.external_name = None
            if not shelf.shown:
                continue
        name = shelf.name or ""
        theirs = conn.execute(
            "SELECT Id FROM Shelf WHERE Name = ? AND _IsDeleted != 'true' AND Id IS NOT ?",
            (name, res.external_id),
        ).fetchone()
        if theirs:
            res.error = f"the Kobo already has a collection called {name!r}, made there"
            continue
        if (
            res.external_id
            and conn.execute("SELECT 1 FROM Shelf WHERE Id = ?", (res.external_id,)).fetchone()
        ):
            conn.execute(
                "UPDATE Shelf SET _IsDeleted = 'false', _IsVisible = 'true',"
                " _IsSynced = 'false', LastModified = ?"
                " WHERE Id = ? AND (_IsDeleted != 'false' OR _IsVisible != 'true')",
                (when, res.external_id),
            )
        else:
            res.external_id = new_id()
            conn.execute(
                "INSERT INTO Shelf (CreationDate, Id, InternalName, LastModified, Name, Type,"
                " _IsDeleted, _IsVisible, _IsSynced, LastAccessed)"
                " VALUES (?, ?, ?, ?, ?, 'UserTag', 'false', 'true', 'false', ?)",
                (when, res.external_id, name, when, name, when),
            )
        res.external_name = name
        wanted = [b for b in shelf.books if b in books_here]
        res.not_on_kobo = len(shelf.books) - len(wanted)
        active = {
            r[0]
            for r in conn.execute(
                "SELECT ContentId FROM ShelfContent WHERE ShelfName = ? AND _IsDeleted != 'true'",
                (name,),
            )
        }
        for book in wanted:
            if book not in active:
                conn.execute(
                    "INSERT OR REPLACE INTO ShelfContent"
                    " (ShelfName, ContentId, DateModified, _IsDeleted, _IsSynced)"
                    " VALUES (?, ?, ?, 'false', 'false')",
                    (name, book, when),
                )
                res.added += 1
        for book in active - set(wanted):
            conn.execute(
                "UPDATE ShelfContent SET _IsDeleted = 'true', _IsSynced = 'false',"
                " DateModified = ? WHERE ShelfName = ? AND ContentId = ?",
                (when, name, book),
            )
            res.removed += 1
        res.written = wanted
    return results


@dataclass
class Plan:
    """A write worked out on the snapshot's copy of the database."""

    snapshot: Snapshot
    when: str
    ids: list[str]  # the new shelf ids, in the order apply() asks for them
    results: list[ShelfResult]
    changes: int  # rows the write changes on the Kobo; 0 means leave it alone


def _copy_db(store: KoboStore, snapshot: Snapshot, folder: Path) -> Path:
    db = snapshot.blob(store, DB_PATH)
    if db is None:
        raise FileNotFoundError(f"snapshot {snapshot.name} has no {DB_PATH}")
    local = folder / "KoboReader.sqlite"
    shutil.copyfile(db, local)
    wal = snapshot.blob(store, WAL)
    if wal is not None:
        shutil.copyfile(wal, folder / "KoboReader.sqlite-wal")
    return local


def plan(store: KoboStore, snapshot: Snapshot, shelves: list[Shelf]) -> Plan:
    """Work out the write on a copy of the snapshot's database."""
    when = now_text()
    ids: list[str] = []

    def new_id() -> str:
        ids.append(str(uuid.uuid4()))
        return ids[-1]

    with tempfile.TemporaryDirectory(prefix="lestash-kobo-shelves-") as tmp:
        conn = sqlite3.connect(_copy_db(store, snapshot, Path(tmp)))
        try:
            before = conn.total_changes
            results = apply(conn, shelves, when, new_id)
            changes = conn.total_changes - before
            conn.commit()
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
    if check != "ok":
        raise KoboDamaged(f"the planned write fails integrity_check on a copy: {check}")
    return Plan(snapshot, when, ids, results, changes)


def _same_as_snapshot(store: KoboStore, snapshot: Snapshot, root: Path) -> None:
    for rel in (DB_PATH, WAL):
        entry = snapshot.files.get(rel)
        here = _file_sha(root / rel)
        if rel == WAL and entry is None and (here is None or (root / rel).stat().st_size == 0):
            continue
        if entry is None or here != entry.sha256:
            raise KoboChanged(
                f"{rel} on the Kobo is not the copy in snapshot {snapshot.name}; "
                "plug in again to take a new snapshot first"
            )


def _restore(store: KoboStore, snapshot: Snapshot, root: Path) -> None:
    """Put the snapshot's database back on the Kobo."""
    for rel in (SHM, WAL):
        (root / rel).unlink(missing_ok=True)
    blob = snapshot.blob(store, DB_PATH)
    assert blob is not None
    shutil.copyfile(blob, root / DB_PATH)
    wal = snapshot.blob(store, WAL)
    if wal is not None:
        shutil.copyfile(wal, root / WAL)


def write(store: KoboStore, the_plan: Plan, shelves: list[Shelf], root: Path) -> list[ShelfResult]:
    """Write a plan to the Kobo mounted read-write at ``root``.

    Raises KoboChanged before writing if the Kobo's database isn't the snapshot's, and
    KoboDamaged (after putting the snapshot's copy back) if the check fails.
    """
    snapshot = the_plan.snapshot
    _same_as_snapshot(store, snapshot, root)
    ids = iter(the_plan.ids)
    conn = sqlite3.connect(root / DB_PATH)
    try:
        results = apply(conn, shelves, the_plan.when, lambda: next(ids))
        conn.commit()
        # Fold the write into the database file, so the Kobo reads one file
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
    except Exception:
        conn.close()
        _restore(store, snapshot, root)
        raise
    conn.close()
    if check != "ok":
        _restore(store, snapshot, root)
        raise KoboDamaged(
            f"integrity_check after the write said {check!r}; put the snapshot's copy back"
        )
    return results
