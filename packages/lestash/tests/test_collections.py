"""Collections: reading order (#317) and what the Kobo should show (#315)."""

import json
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from lestash.core.collections import (
    KOBO,
    add_item,
    kobo_books,
    kobo_shelves,
    kobo_status,
    move_item,
    ordered_item_ids,
    record_written,
    set_shown,
)
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import get_connection, init_database


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    with tempfile.TemporaryDirectory() as tmp:
        config = Config(general=GeneralConfig(database_path=str(Path(tmp) / "t.db")))
        init_database(config)
        with get_connection(config) as c:
            yield c


def _item(conn: sqlite3.Connection, source_type: str, source_id: str, **meta) -> int:
    cur = conn.execute(
        "INSERT INTO items (source_type, source_id, title, content, metadata)"
        " VALUES (?, ?, ?, 'x', ?)",
        (source_type, source_id, source_id, json.dumps(meta) if meta else None),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def _collection(conn: sqlite3.Connection, name: str = "0 Introducing ontology") -> int:
    cur = conn.execute("INSERT INTO collections (name) VALUES (?)", (name,))
    assert cur.lastrowid is not None
    return cur.lastrowid


def test_items_keep_the_order_they_were_added_in(conn) -> None:
    c = _collection(conn)
    ids = [_item(conn, "kobo", f"file:///mnt/onboard/{n}.pdf") for n in "abc"]
    for i in ids:
        assert add_item(conn, c, i)
    assert not add_item(conn, c, ids[0])  # already there
    assert ordered_item_ids(conn, c) == ids


def test_move_puts_an_item_in_place_and_numbers_the_rest(conn) -> None:
    c = _collection(conn)
    a, b, d = (_item(conn, "kobo", f"file:///mnt/onboard/{n}.pdf") for n in "abd")
    for i in (a, b, d):
        add_item(conn, c, i)
    assert move_item(conn, c, d, 1)
    assert ordered_item_ids(conn, c) == [d, a, b]
    assert move_item(conn, c, d, 99)  # past the end: last
    assert ordered_item_ids(conn, c) == [a, b, d]
    assert not move_item(conn, c, 12345, 1)


def test_items_added_before_positions_come_after_placed_ones(conn) -> None:
    c = _collection(conn)
    old, new = _item(conn, "arxiv", "1"), _item(conn, "arxiv", "2")
    conn.execute("INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)", (c, old))
    add_item(conn, c, new)
    assert ordered_item_ids(conn, c) == [new, old]


def test_kobo_books_are_kobo_items_or_their_alternates(conn) -> None:
    c = _collection(conn)
    book = _item(conn, "kobo", "file:///mnt/onboard/a.pdf")
    paper = _item(conn, "arxiv", "2401.1")
    copy = _item(conn, "kobo", "file:///mnt/onboard/b.pdf")
    gone = _item(conn, "kobo", "file:///mnt/onboard/old.pdf", removed_from_device="2026-11-14")
    note = _item(conn, "kobo", "bookmark:hl-1")
    conn.execute(
        "INSERT INTO item_links (from_id, to_id, kind, origin, status)"
        " VALUES (?, ?, 'alternate_of', 'manual', 'confirmed')",
        (copy, paper),
    )
    for i in (book, paper, gone, note):
        add_item(conn, c, i)
    assert kobo_books(conn, c) == {
        "file:///mnt/onboard/a.pdf": book,
        "file:///mnt/onboard/b.pdf": paper,
    }


def test_status_counts_what_is_on_waiting_and_missing(conn) -> None:
    c = _collection(conn)
    assert kobo_status(conn, c) is None
    a, b = (_item(conn, "kobo", f"file:///mnt/onboard/{n}.pdf") for n in "ab")
    other = _item(conn, "youtube", "video:1")
    for i in (a, b, other):
        add_item(conn, c, i)
    set_shown(conn, c, KOBO, True)
    st = kobo_status(conn, c)
    assert st and (st.on, st.waiting, st.missing) == (0, 2, 1)
    (shelf,) = kobo_shelves(conn)
    record_written(
        conn,
        shelf,
        external_id="id-1",
        external_name="0 Introducing ontology",
        written=["file:///mnt/onboard/a.pdf", "file:///mnt/onboard/b.pdf"],
        when="2026-11-14T13:00:00Z",
    )
    st = kobo_status(conn, c)
    assert st and (st.on, st.waiting, st.missing) == (2, 0, 1)
    assert st.text() == "2 on the Kobo · 1 not on the Kobo"
    set_shown(conn, c, KOBO, False)
    st = kobo_status(conn, c)
    assert st and not st.shown and st.waiting == 2  # two to take off


def test_shelves_list_books_in_reading_order(conn) -> None:
    c = _collection(conn)
    a, b = (_item(conn, "kobo", f"file:///mnt/onboard/{n}.pdf") for n in "ab")
    add_item(conn, c, a)
    add_item(conn, c, b)
    move_item(conn, c, b, 1)
    set_shown(conn, c, KOBO, True)
    (shelf,) = kobo_shelves(conn)
    assert shelf.shown and shelf.name == "0 Introducing ontology"
    assert shelf.books == ["file:///mnt/onboard/b.pdf", "file:///mnt/onboard/a.pdf"]


def test_a_deleted_collection_keeps_its_place_until_written(conn) -> None:
    c = _collection(conn)
    set_shown(conn, c, KOBO, True)
    conn.execute("UPDATE collection_places SET external_id = 'id-1', external_name = 'x'")
    conn.execute("DELETE FROM collections WHERE id = ?", (c,))
    (shelf,) = kobo_shelves(conn)
    assert shelf.collection_id is None and not shelf.shown and shelf.external_id == "id-1"
    record_written(conn, shelf, external_id=None, external_name=None, written=[], when="t")
    assert kobo_shelves(conn) == []
