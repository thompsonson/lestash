"""Notes drawn on PDFs on the Kobo, read from each annotated state (#338)."""

from datetime import UTC, datetime
from pathlib import Path

import pymupdf
import pytest
from lestash.core.database import get_connection
from lestash.core.sync import SyncStore
from lestash_kobo import KoboSource
from lestash_kobo.notes import extract_notes
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
    rows = conn.execute(
        "SELECT json_extract(metadata, '$.first_seen_in'),"
        " json_extract(metadata, '$.removed_from_pdf') IS NOT NULL"
        " FROM items WHERE parent_id = ? AND source_type = 'pdf_annotation' ORDER BY id",
        (book,),
    ).fetchall()
    return [tuple(r) for r in rows]


def _annotated(conn, book: int) -> list[tuple]:
    rows = conn.execute(
        "SELECT occurred_at, json_extract(details, '$.notes') FROM activities"
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
    assert "2 new notes on 1 books" in result.output
    with get_connection(test_db) as conn:
        (status, added) = conn.execute(
            "SELECT status, items_added FROM sync_log WHERE source_type = 'kobo-notes'"
        ).fetchone()
    assert (status, added) == ("completed", 2)
