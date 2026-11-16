"""Notes drawn on PDFs on the Kobo, read from each annotated state (#338)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pymupdf
import pytest
from lestash.core.database import get_connection
from lestash.core.sync import SyncStore
from lestash_kobo import KoboSource
from lestash_kobo.notes import extract_notes, tool
from lestash_kobo.source import _store
from typer.testing import CliRunner

PAPER = "Research/Typing.pdf"


def _at(minute: int) -> datetime:
    return datetime(2026, 11, 14, 10, minute, tzinfo=UTC)


def _pdf(strokes: int) -> bytes:
    """A two-page PDF with an ink mark on each of its first ``strokes`` pages."""
    doc = pymupdf.open()
    for n in range(2):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {n + 1}: types are sets of values.")
        if n < strokes:
            page.add_ink_annot([[(100, 150), (160, 160), (220, 150)]])
    data = doc.tobytes()
    doc.close()
    return data


def _plug_in(device: Path, test_db, minute: int, strokes: int) -> None:
    (device / PAPER).parent.mkdir(exist_ok=True)
    (device / PAPER).write_bytes(_pdf(strokes))
    _store({}, test_db).take(device, _at(minute))
    with get_connection(test_db) as conn:
        SyncStore(conn, "kobo").run(KoboSource().sync({}))


def _book(conn) -> int:
    return conn.execute(
        "SELECT id FROM items WHERE source_type = 'kobo' AND source_id LIKE ?",
        (f"%{PAPER}",),
    ).fetchone()[0]


def _notes(conn, book: int) -> list[tuple]:
    """Each note: the state it was first read from, and whether it was erased."""
    first = {}
    for sha, text in conn.execute(
        "SELECT sha256, text FROM blob_text WHERE tool = ?", (tool(book),)
    ):
        for note in json.loads(text)["new_notes"]:
            first[note] = sha
    rows = conn.execute(
        "SELECT id, json_extract(metadata, '$.removed_from_pdf') IS NOT NULL"
        " FROM items WHERE parent_id = ? AND source_type = 'pdf_annotation' ORDER BY id",
        (book,),
    ).fetchall()
    return [(first.get(r[0]), r[1]) for r in rows]


def _annotated(conn, book: int) -> list[tuple]:
    rows = conn.execute(
        "SELECT occurred_at, json_array_length(details, '$.notes') FROM activities"
        " WHERE item_id = ? AND verb = 'annotated' ORDER BY occurred_at",
        (book,),
    ).fetchall()
    return [tuple(r) for r in rows]


@pytest.fixture
def three_states(device, test_db):
    """A paper plugged in clean, then with one mark, then with two."""
    for minute, strokes in ((1, 0), (2, 1), (3, 2)):
        _plug_in(device, test_db, minute, strokes)


def test_each_state_adds_only_its_new_notes(three_states, test_db) -> None:
    with get_connection(test_db) as conn:
        result = extract_notes(conn, test_db)
        book = _book(conn)
        states = [
            r[0]
            for r in conn.execute(
                "SELECT sha256 FROM item_media WHERE item_id = ? AND media_type = 'pdf'"
                " ORDER BY version",
                (book,),
            )
        ]
        notes = _notes(conn, book)
        annotated = _annotated(conn, book)
    # The test Kobo's Drive PDF is a fourth state, with no notes
    assert result.states == 4 and result.added == 2 and not result.errors
    assert notes == [(states[1], 0), (states[2], 0)]
    assert [n for _, n in annotated] == [1, 1]  # the clean state records nothing


def test_states_already_read_are_not_read_again(three_states, test_db) -> None:
    with get_connection(test_db) as conn:
        extract_notes(conn, test_db)
        again = extract_notes(conn, test_db)
    assert again.states == 0 and again.added == 0


def test_an_erased_note_is_kept_and_marked(device, test_db, three_states) -> None:
    with get_connection(test_db) as conn:
        extract_notes(conn, test_db)
    _plug_in(device, test_db, 4, 1)  # the second mark erased
    with get_connection(test_db) as conn:
        result = extract_notes(conn, test_db)
        notes = _notes(conn, _book(conn))
    assert result.added == 0
    assert [removed for _, removed in notes] == [0, 1]


def test_an_unreadable_pdf_is_recorded_not_retried(device, test_db) -> None:
    (device / "Research").mkdir()
    (device / PAPER).write_bytes(b"%PDF-1.4 not really")
    _store({}, test_db).take(device, _at(1))
    with get_connection(test_db) as conn:
        SyncStore(conn, "kobo").run(KoboSource().sync({}))
        first = extract_notes(conn, test_db)
        again = extract_notes(conn, test_db)
    assert first.states == 2 and first.added == 0 and not first.errors  # and Drive's
    assert again.states == 0


def test_the_command_reports_and_logs(three_states, test_db) -> None:
    result = CliRunner().invoke(KoboSource().get_commands(), ["extract-notes"])
    assert result.exit_code == 0, result.output
    assert "2 new notes on 1 books" in " ".join(result.output.split())  # Rich wraps
    with get_connection(test_db) as conn:
        (status, added) = conn.execute(
            "SELECT status, items_added FROM sync_log WHERE source_type = 'kobo-notes'"
        ).fetchone()
    assert (status, added) == ("completed", 2)


def test_the_same_bytes_on_two_books_are_two_states(device, test_db) -> None:
    (device / "Research").mkdir()
    (device / "Research" / "Copy.pdf").write_bytes(_pdf(1))
    _plug_in(device, test_db, 1, 1)
    with get_connection(test_db) as conn:
        result = extract_notes(conn, test_db)
    assert result.added == 2 and len(result.books) == 2


def test_an_older_state_read_late_does_not_erase_newer_notes(three_states, test_db) -> None:
    with get_connection(test_db) as conn:
        book = _book(conn)
        extract_notes(conn, test_db)
        # The first marked state forgotten, as if a file had been missing for it
        (sha,) = conn.execute(
            "SELECT sha256 FROM item_media WHERE item_id = ? AND media_type = 'pdf'"
            " ORDER BY created_at LIMIT 1 OFFSET 1",
            (book,),
        ).fetchone()
        conn.execute("DELETE FROM blob_text WHERE sha256 = ? AND tool = ?", (sha, tool(book)))
        conn.commit()
        result = extract_notes(conn, test_db)
        notes = _notes(conn, book)
    assert result.states == 0
    assert [removed for _, removed in notes] == [0, 0]


def test_a_newer_extractor_reads_only_the_newest_state(three_states, test_db) -> None:
    with get_connection(test_db) as conn:
        book = _book(conn)
        extract_notes(conn, test_db)
        conn.execute("UPDATE blob_text SET tool_version = '0' WHERE tool = ?", (tool(book),))
        conn.commit()
        result = extract_notes(conn, test_db)
        removed = conn.execute(
            "SELECT COUNT(*) FROM items WHERE parent_id = ?"
            " AND json_extract(metadata, '$.removed_from_pdf') IS NOT NULL",
            (book,),
        ).fetchone()[0]
    assert result.added == 0 and removed == 0


def test_a_file_put_back_to_earlier_bytes_is_read_again(device, test_db, three_states) -> None:
    with get_connection(test_db) as conn:
        extract_notes(conn, test_db)
    _plug_in(device, test_db, 4, 1)  # back to the one-mark bytes
    with get_connection(test_db) as conn:
        result = extract_notes(conn, test_db)
        notes = _notes(conn, _book(conn))
    assert result.states == 1
    assert [removed for _, removed in notes] == [0, 1]  # the second mark erased


def test_a_file_missing_from_the_store_is_passed_over(device, test_db, three_states) -> None:
    from lestash.core.blobs import BlobStore, store_root

    with get_connection(test_db) as conn:
        book = _book(conn)
        (sha,) = conn.execute(
            "SELECT sha256 FROM item_media WHERE item_id = ? AND media_type = 'pdf'"
            " ORDER BY created_at LIMIT 1 OFFSET 1",
            (book,),
        ).fetchone()
        BlobStore(store_root(test_db)).path(sha).unlink()
        conn.execute("UPDATE item_media SET local_path = NULL WHERE sha256 = ?", (sha,))
        conn.commit()
        first = extract_notes(conn, test_db)
        again = extract_notes(conn, test_db)
    assert any("not in the store" in m for m in first.missing) and not first.errors
    assert first.added == 2  # the newest state still has both marks
    assert again.states == 0 and not again.missing  # a newer state was read: passed for good


def test_a_missing_newest_state_is_read_once_its_file_is_back(device, test_db) -> None:
    from lestash.core.blobs import BlobStore, store_root

    _plug_in(device, test_db, 1, 1)
    with get_connection(test_db) as conn:
        book = _book(conn)
        (sha,) = conn.execute(
            "SELECT sha256 FROM item_media WHERE item_id = ? AND media_type = 'pdf'", (book,)
        ).fetchone()
        blob = BlobStore(store_root(test_db)).path(sha)
        kept = blob.read_bytes()
        blob.unlink()
        conn.execute("UPDATE item_media SET local_path = NULL WHERE sha256 = ?", (sha,))
        conn.commit()
        first = extract_notes(conn, test_db)
        blob.write_bytes(kept)
        back = extract_notes(conn, test_db)
    assert first.missing and first.added == 0
    assert back.added == 1 and not back.missing


def test_states_since_an_older_extractor_are_all_read(device, test_db) -> None:
    for minute, strokes in ((1, 0), (2, 1)):
        _plug_in(device, test_db, minute, strokes)
    with get_connection(test_db) as conn:
        extract_notes(conn, test_db)
        conn.execute("UPDATE blob_text SET tool_version = '0' WHERE tool = ?", (tool(_book(conn)),))
        conn.commit()
    _plug_in(device, test_db, 3, 2)  # arrives after the upgrade
    with get_connection(test_db) as conn:
        result = extract_notes(conn, test_db)
    assert result.states == 2 and result.added == 1  # the old newest again, then the new
