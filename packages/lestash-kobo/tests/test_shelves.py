"""Writing LeStash collections to the Kobo (K8, #315)."""

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from lestash.core.collections import KOBO, add_item, kobo_status, set_shown
from lestash.core.database import get_connection
from lestash.core.sync import SyncStore
from lestash_kobo import KoboSource
from lestash_kobo import shelves as kobo_shelf
from lestash_kobo.source import _store
from typer.testing import CliRunner

from .conftest import DRIVE_PDF, NN

DB = Path(".kobo/KoboReader.sqlite")


def _at(minute: int) -> datetime:
    return datetime(2026, 11, 14, 12, minute, tzinfo=UTC)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def kobo(device: Path, test_db):
    """The test Kobo imported once, and a collection holding both its books."""
    store = _store({}, test_db)
    store.take(device, _at(0))
    with get_connection(test_db) as conn:
        SyncStore(conn, "kobo").run(KoboSource().sync({}))
        cid = conn.execute("INSERT INTO collections (name) VALUES ('0 Intro')").lastrowid
        for source_id in (DRIVE_PDF, NN):
            item = conn.execute("SELECT id FROM items WHERE source_id = ?", (source_id,))
            add_item(conn, cid, item.fetchone()[0])
        conn.commit()
    return store, cid


def _write(device: Path, *args: str):
    app = KoboSource().get_commands()
    return CliRunner().invoke(app, ["write-collections", "--root", str(device), *args])


def _shelves(device: Path) -> dict:
    conn = sqlite3.connect(device / DB)
    try:
        shelves = {
            r[0]: r[1:] for r in conn.execute("SELECT Name, Id, _IsDeleted, Type FROM Shelf")
        }
        content = sorted(
            conn.execute("SELECT ShelfName, ContentId, _IsDeleted FROM ShelfContent").fetchall()
        )
    finally:
        conn.close()
    return {"shelves": shelves, "content": content}


def _show(test_db, cid: int, shown: bool = True) -> None:
    with get_connection(test_db) as conn:
        set_shown(conn, cid, KOBO, shown)
        conn.commit()


def test_nothing_is_written_until_a_collection_is_switched_on(device, kobo) -> None:
    before = _sha(device / DB)
    result = _write(device)
    assert result.exit_code == 0, result.output
    assert "nothing to write" in result.output
    assert _sha(device / DB) == before


def test_a_collection_switched_on_is_written_with_its_books(device, kobo, test_db) -> None:
    store, cid = kobo
    _show(test_db, cid)
    result = _write(device)
    assert result.exit_code == 0, result.output
    got = _shelves(device)
    shelf_id, deleted, kind = got["shelves"]["0 Intro"]
    assert (deleted, kind) == ("false", "UserTag")
    assert ("0 Intro", DRIVE_PDF, "false") in got["content"]
    assert ("0 Intro", NN, "false") in got["content"]
    # The collection made on the Kobo is untouched
    assert got["shelves"]["Formal Stuff"] == ("kobo-made-1", "false", "UserTag")
    assert ("Formal Stuff", DRIVE_PDF, "false") in got["content"]
    with get_connection(test_db) as conn:
        st = kobo_status(conn, cid)
        assert st and (st.on, st.waiting) == (2, 0) and st.last_written
        log = conn.execute(
            "SELECT status, items_added FROM sync_log WHERE source_type = 'kobo-collections'"
        ).fetchall()
    assert [tuple(r) for r in log] == [("completed", 2)]
    # No WAL left beside it: the Kobo reads one file
    assert (
        not (device / ".kobo/KoboReader.sqlite-wal").exists()
        or not (device / ".kobo/KoboReader.sqlite-wal").stat().st_size
    )


def test_a_second_plug_in_writes_nothing(device, kobo, test_db) -> None:
    store, cid = kobo
    _show(test_db, cid)
    assert _write(device).exit_code == 0
    store.take(device, _at(1))
    before = _sha(device / DB)
    result = _write(device)
    assert result.exit_code == 0, result.output
    assert "nothing to write" in result.output
    assert _sha(device / DB) == before


def test_removing_a_book_and_hiding_the_collection(device, kobo, test_db) -> None:
    store, cid = kobo
    _show(test_db, cid)
    assert _write(device).exit_code == 0
    with get_connection(test_db) as conn:
        conn.execute(
            "DELETE FROM collection_items WHERE collection_id = ? AND item_id ="
            " (SELECT id FROM items WHERE source_id = ?)",
            (cid, NN),
        )
        conn.commit()
    store.take(device, _at(1))
    assert _write(device).exit_code == 0
    assert ("0 Intro", NN, "true") in _shelves(device)["content"]
    _show(test_db, cid, False)
    store.take(device, _at(2))
    assert _write(device).exit_code == 0
    got = _shelves(device)
    assert got["shelves"]["0 Intro"][1] == "true"
    assert ("0 Intro", DRIVE_PDF, "true") in got["content"]
    assert got["shelves"]["Formal Stuff"][1] == "false"


def test_a_kobo_collection_with_the_same_name_is_left_alone(device, kobo, test_db) -> None:
    store, cid = kobo
    with get_connection(test_db) as conn:
        conn.execute("UPDATE collections SET name = 'Formal Stuff' WHERE id = ?", (cid,))
        conn.commit()
    _show(test_db, cid)
    before = _sha(device / DB)
    result = _write(device)
    assert result.exit_code == 0, result.output
    assert "made there" in result.output
    assert _sha(device / DB) == before
    with get_connection(test_db) as conn:
        st = kobo_status(conn, cid)
        assert st and st.error and "made there" in st.error


def test_a_kobo_changed_since_the_snapshot_is_not_written(device, kobo, test_db) -> None:
    store, cid = kobo
    _show(test_db, cid)
    conn = sqlite3.connect(device / DB)
    conn.execute("UPDATE content SET Title = 'changed' WHERE ContentID = ?", (NN,))
    conn.commit()
    conn.close()
    before = _sha(device / DB)
    result = _write(device)
    assert result.exit_code == 1
    assert "not the copy in snapshot" in result.output
    assert _sha(device / DB) == before
    with get_connection(test_db) as conn:
        (status,) = conn.execute(
            "SELECT status FROM sync_log WHERE source_type = 'kobo-collections'"
        ).fetchone()
    assert status == "failed"


def test_a_failed_write_puts_the_snapshot_back(device, kobo, test_db, monkeypatch) -> None:
    store, cid = kobo
    _show(test_db, cid)
    before = _sha(device / DB)
    real = kobo_shelf.apply

    def half_then_fail(conn, shelves, when, new_id):
        real(conn, shelves, when, new_id)
        conn.commit()  # a write that lands, then a failure
        raise sqlite3.DatabaseError("disk I/O error")

    calls = iter([real, half_then_fail])
    monkeypatch.setattr(kobo_shelf, "apply", lambda *a: next(calls)(*a))
    result = _write(device)
    assert result.exit_code == 1, result.output
    assert _sha(device / DB) == before
    assert "Formal Stuff" in _shelves(device)["shelves"]


def test_dry_run_touches_nothing(device, kobo, test_db) -> None:
    store, cid = kobo
    _show(test_db, cid)
    before = _sha(device / DB)
    result = _write(device, "--dry-run")
    assert result.exit_code == 0, result.output
    assert "0 Intro: 2 books, 2 to add" in result.output
    assert _sha(device / DB) == before


def test_status_and_switch_commands(device, kobo, test_db) -> None:
    store, cid = kobo
    app = KoboSource().get_commands()
    runner = CliRunner()
    assert "No collection is shown" in runner.invoke(app, ["collections"]).output
    shown = runner.invoke(app, ["show", "0 Intro"])
    assert shown.exit_code == 0, shown.output
    assert "0 Intro: on; 0 on the Kobo · 2 waiting" in runner.invoke(app, ["collections"]).output
    assert runner.invoke(app, ["show", "missing"]).exit_code == 1


def test_a_failed_mount_is_logged_not_a_traceback(device, kobo, test_db, monkeypatch) -> None:
    import subprocess

    from lestash_kobo import collections_cli

    store, cid = kobo
    _show(test_db, cid)

    def fake_run(cmd, **kw):
        code = 1 if cmd[0] == collections_cli.MOUNTPOINT else 32
        return subprocess.CompletedProcess(cmd, code, "", "mount: permission denied")

    monkeypatch.setattr(collections_cli.subprocess, "run", fake_run)
    result = _write(device, "--mount")
    assert result.exit_code == 1, result.output
    assert "Nothing written: could not mount" in result.output
    with get_connection(test_db) as conn:
        st = kobo_status(conn, cid)
        (status,) = conn.execute(
            "SELECT status FROM sync_log WHERE source_type = 'kobo-collections'"
        ).fetchone()
    assert st and st.error and "could not mount" in st.error
    assert status == "failed"
