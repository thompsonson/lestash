"""PDFs asked for on the Kobo, copied at plug-in, and matched back (#338)."""

import hashlib
import shutil
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lestash.core.collections import KOBO, add_item, kobo_books, set_shown
from lestash.core.database import get_connection
from lestash.core.item_places import item_place, set_wanted
from lestash.core.pdfs import add_pdf, link_revision
from lestash.core.sync import SyncStore
from lestash_kobo import KoboSource
from lestash_kobo.files_cli import file_name
from lestash_kobo.source import _store
from typer.testing import CliRunner


def _at(minute: int) -> datetime:
    return datetime(2026, 11, 14, 9, minute, tzinfo=UTC)


@pytest.fixture(autouse=True)
def copy_clock(test_db, monkeypatch):
    """A copy is made just after the newest snapshot, as at a plug-in: test snapshots
    are dated in the past, so the real clock would put every copy after them."""
    import lestash_kobo.files_cli as files

    def now() -> str:
        latest = _store({}, test_db).latest()
        when = latest.taken_at + timedelta(seconds=30) if latest else _at(0)
        return when.strftime("%Y-%m-%dT%H:%M:%SZ")

    monkeypatch.setattr(files, "now_text", now)


def _cli(*args: str):
    return CliRunner().invoke(KoboSource().get_commands(), list(args))


def _copy(device: Path, *args: str):
    return _cli("copy-files", "--root", str(device), *args)


def _pdf(test_db, data: bytes, name: str) -> int:
    with get_connection(test_db) as conn:
        added = add_pdf(conn, test_db, data, name, extract=False)
        conn.commit()
    return added.item_id


@pytest.fixture
def paper(test_db) -> int:
    item = _pdf(test_db, b"%PDF-1.4 typing v1", "Typing an Ontology.pdf")
    with get_connection(test_db) as conn:
        set_wanted(conn, item, KOBO, True)
        conn.commit()
    return item


def _import(device: Path, test_db, minute: int) -> None:
    _store({}, test_db).take(device, _at(minute))
    with get_connection(test_db) as conn:
        SyncStore(conn, "kobo").run(KoboSource().sync({}))


def test_file_names_are_safe_on_the_kobo() -> None:
    assert file_name('A: "B" / C?') == "A B C.pdf"
    assert file_name("...") == "Untitled.pdf"
    assert len(file_name("x" * 300)) == 124


def test_an_item_asked_for_is_copied_checked_and_recorded(device, test_db, paper) -> None:
    result = _copy(device)
    assert result.exit_code == 0, result.output
    copy = device / "LeStash" / "Typing an Ontology.pdf"
    assert copy.read_bytes() == b"%PDF-1.4 typing v1"
    assert not list((device / "LeStash").glob(".*"))
    with get_connection(test_db) as conn:
        place = item_place(conn, paper)
        (status,) = conn.execute(
            "SELECT status FROM sync_log WHERE source_type = 'kobo-files'"
        ).fetchone()
    assert place.path == "LeStash/Typing an Ontology.pdf" and place.copied_at
    assert place.text() == "Copied, waiting for the Kobo to import it"
    assert status == "completed"
    again = _copy(device)  # before the Kobo imports it: no second copy
    assert again.exit_code == 0 and "already on the Kobo as" in again.output
    assert len(list((device / "LeStash").iterdir())) == 1


def test_once_imported_the_book_is_linked_and_shown_in_collections(device, test_db, paper) -> None:
    _copy(device)
    _import(device, test_db, 1)
    with get_connection(test_db) as conn:
        assert item_place(conn, paper).text() == "On the Kobo"
        book = conn.execute(
            "SELECT from_id FROM item_links WHERE to_id = ? AND kind = 'alternate_of'",
            (paper,),
        ).fetchone()
        assert book is not None
        cid = conn.execute("INSERT INTO collections (name) VALUES ('Learning')").lastrowid
        add_item(conn, cid, paper)
        set_shown(conn, cid, KOBO, True)
        conn.commit()
        assert set(kobo_books(conn, cid).values()) == {paper}
    assert "Nothing to copy" in _copy(device).output


def test_annotating_the_copy_keeps_the_link(device, test_db, paper) -> None:
    _copy(device)
    _import(device, test_db, 1)
    (device / "LeStash" / "Typing an Ontology.pdf").write_bytes(b"%PDF-1.4 typing v1 + ink")
    _import(device, test_db, 2)
    with get_connection(test_db) as conn:
        assert item_place(conn, paper).on


def test_only_the_newest_version_is_copied(device, test_db, paper) -> None:
    newer = _pdf(test_db, b"%PDF-1.4 typing v2", "Typing an Ontology v2.pdf")
    with get_connection(test_db) as conn:
        link_revision(conn, newer, paper)
        conn.commit()
    assert _copy(device).exit_code == 0
    assert [p.name for p in (device / "LeStash").iterdir()] == ["Typing an Ontology v2.pdf"]


def test_a_file_already_on_the_kobo_is_never_overwritten(device, test_db, paper) -> None:
    taken = device / "LeStash" / "Typing an Ontology.pdf"
    taken.parent.mkdir()
    taken.write_bytes(b"%PDF someone else's")
    assert _copy(device).exit_code == 0
    assert taken.read_bytes() == b"%PDF someone else's"
    sha = hashlib.sha256(b"%PDF-1.4 typing v1").hexdigest()
    other = device / "LeStash" / f"Typing an Ontology ({sha[:8]}).pdf"
    assert other.read_bytes() == b"%PDF-1.4 typing v1"


def test_a_copy_sideloaded_another_way_is_not_copied_again(device, test_db, paper) -> None:
    research = device / "Research" / "Typing.pdf"
    research.parent.mkdir()
    research.write_bytes(b"%PDF-1.4 typing v1")
    _store({}, test_db).take(device, _at(1))
    result = _copy(device)
    assert result.exit_code == 0
    assert "on the Kobo as Research/Typing.pdf" in result.output
    assert not (device / "LeStash").exists()


def test_no_room_copies_nothing_and_says_why(device, test_db, paper, monkeypatch) -> None:
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(100, 100, 0))
    result = _copy(device)
    assert result.exit_code == 1
    assert "not enough space" in result.output
    assert not (device / "LeStash").exists()
    with get_connection(test_db) as conn:
        assert "not enough space" in (item_place(conn, paper).error or "")


def test_dry_run_touches_nothing(device, test_db, paper) -> None:
    result = _copy(device, "--dry-run")
    assert result.exit_code == 0 and "to copy" in result.output
    assert not (device / "LeStash").exists()


def test_a_marked_item_without_a_pdf_is_reported(device, test_db) -> None:
    with get_connection(test_db) as conn:
        (item,) = conn.execute(
            "INSERT INTO items (source_type, source_id, content) VALUES ('note', 'n1', 'x')"
            " RETURNING id"
        ).fetchone()
        set_wanted(conn, item, KOBO, True)
        conn.commit()
    result = _copy(device)
    assert "has no PDF file" in result.output


def test_send_unsend_and_files(device, test_db) -> None:
    item = _pdf(test_db, b"%PDF-1.4 x", "X.pdf")
    assert "For the Kobo" in _cli("send", str(item)).output
    assert f"{item}  X  (you)" in _cli("files").output
    assert "Not on the Kobo" in _cli("unsend", str(item)).output
    assert "No item is asked for" in _cli("files").output
    assert _cli("send", "99999").exit_code == 1


def test_a_collection_of_an_older_version_shows_the_newest_copy(device, test_db, paper) -> None:
    newer = _pdf(test_db, b"%PDF-1.4 typing v2", "Typing an Ontology v2.pdf")
    with get_connection(test_db) as conn:
        link_revision(conn, newer, paper)
        set_wanted(conn, paper, KOBO, False)
        cid = conn.execute("INSERT INTO collections (name) VALUES ('Learning')").lastrowid
        add_item(conn, cid, paper)  # the collection holds the first version
        set_shown(conn, cid, KOBO, True)
        conn.commit()
    _copy(device)
    _import(device, test_db, 1)
    with get_connection(test_db) as conn:
        books = kobo_books(conn, cid)
    assert list(books.values()) == [paper]
    assert next(iter(books)).endswith("LeStash/Typing an Ontology v2.pdf")


def test_a_copy_taken_off_the_kobo_stays_off_until_sent_again(device, test_db, paper) -> None:
    _copy(device)
    _import(device, test_db, 1)
    (device / "LeStash" / "Typing an Ontology.pdf").unlink()
    _import(device, test_db, 2)
    with get_connection(test_db) as conn:
        assert item_place(conn, paper).text().startswith("Taken off the Kobo")
    assert "Nothing to copy" in _copy(device).output
    assert "copied at the next plug-in" in _cli("send", str(paper)).output
    assert _copy(device).exit_code == 0
    assert (device / "LeStash" / "Typing an Ontology.pdf").is_file()


def test_a_failed_copy_leaves_no_part_file(device, test_db, paper, monkeypatch) -> None:
    import lestash_kobo.files_cli as files

    monkeypatch.setattr(files, "_sha", lambda path: "0" * 64)
    result = _copy(device)
    assert result.exit_code == 1 and "doesn't match" in result.output
    assert list((device / "LeStash").iterdir()) == []


def test_a_copy_left_unfinished_by_a_stopped_run_is_replaced(device, test_db, paper) -> None:
    left = device / "LeStash" / ".Typing an Ontology.pdf.lestash-part"
    left.parent.mkdir()
    left.write_bytes(b"%PDF-1.4 typ")  # a run stopped mid-copy
    assert _copy(device).exit_code == 0
    assert [p.name for p in (device / "LeStash").iterdir()] == ["Typing an Ontology.pdf"]


def test_sent_again_after_being_taken_off_reads_as_copied(device, test_db, paper) -> None:
    _copy(device)
    _import(device, test_db, 1)
    (device / "LeStash" / "Typing an Ontology.pdf").unlink()
    _import(device, test_db, 2)
    _cli("send", str(paper))
    _copy(device)
    with get_connection(test_db) as conn:
        assert item_place(conn, paper).text() == "Copied, waiting for the Kobo to import it"


def test_an_older_version_with_its_own_copy_shows_the_newest(device, test_db, paper) -> None:
    with get_connection(test_db) as conn:
        cid = conn.execute("INSERT INTO collections (name) VALUES ('Learning')").lastrowid
        add_item(conn, cid, paper)
        set_shown(conn, cid, KOBO, True)
        conn.commit()
    _copy(device)  # the first version's copy
    _import(device, test_db, 1)
    newer = _pdf(test_db, b"%PDF-1.4 typing v2", "Typing an Ontology v2.pdf")
    with get_connection(test_db) as conn:
        link_revision(conn, newer, paper)
        conn.commit()
    _copy(device)
    _import(device, test_db, 2)
    with get_connection(test_db) as conn:
        books = kobo_books(conn, cid)
    assert list(books.values()) == [paper]
    assert next(iter(books)).endswith("LeStash/Typing an Ontology v2.pdf")
