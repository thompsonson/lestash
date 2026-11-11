"""Stub items: made once per identifier, hidden from listings, merged when filled in."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from lestash.core.activities import record_activities
from lestash.core.database import SCHEMA_VERSION, apply_migrations, get_connection
from lestash.core.search import keyword_search
from lestash.core.stubs import make_stub, merge_stub
from lestash.models.item import ActivityCreate

WHEN = datetime(2026, 1, 1, tzinfo=UTC)


def _item(conn: sqlite3.Connection, source_id: str, content: str = "held text") -> int:
    return conn.execute(
        "INSERT INTO items (source_type, source_id, content) VALUES ('linkedin', ?, ?) "
        "RETURNING id",
        (source_id, content),
    ).fetchone()[0]


def _link(conn: sqlite3.Connection, from_id: int, to_id: int, kind: str) -> None:
    conn.execute(
        "INSERT INTO item_links (from_id, to_id, kind, origin, status) "
        "VALUES (?, ?, ?, 'test', 'confirmed')",
        (from_id, to_id, kind),
    )


def test_migration_18_marks_every_stored_item_held(test_db) -> None:
    with get_connection(test_db) as conn:
        # A version 17 database has neither column; kind (24) is built on held
        conn.execute("DROP INDEX idx_items_kind")
        conn.execute("ALTER TABLE items DROP COLUMN kind")
        conn.execute("ALTER TABLE items DROP COLUMN held")
        conn.execute("PRAGMA user_version = 17")
        _item(conn, "a")
        conn.commit()
        assert apply_migrations(conn) == SCHEMA_VERSION - 17
        assert conn.execute("SELECT held FROM items").fetchone()[0] == 1


def test_a_stub_is_made_once_and_hidden_from_search(test_db) -> None:
    with get_connection(test_db) as conn:
        stub = make_stub(conn, "linkedin", "urn:li:activity:1", url="https://x.example/1")
        assert make_stub(conn, "linkedin", "urn:li:activity:1") == stub
        row = conn.execute(
            "SELECT source_id, content, held, url FROM items WHERE id = ?", (stub,)
        ).fetchone()
        assert tuple(row) == ("stub:urn:li:activity:1", "", 0, "https://x.example/1")
        conn.execute("UPDATE items SET title = 'findable' WHERE id = ?", (stub,))
        assert keyword_search(conn, "findable", limit=10) == []


def test_merging_moves_links_and_activities_onto_the_held_item(test_db) -> None:
    with get_connection(test_db) as conn:
        stub = make_stub(conn, "linkedin", "urn:li:activity:1")
        post, comment, other = _item(conn, "post"), _item(conn, "comment"), _item(conn, "other")
        _link(conn, comment, stub, "in_reply_to")
        _link(conn, other, stub, "references")
        _link(conn, other, post, "references")  # already there: the stub's copy goes
        _link(conn, post, stub, "references")  # becomes a link to itself: dropped
        like = ActivityCreate(verb="liked", occurred_at=WHEN, source="linkedin")
        record_activities(conn, stub, [like])
        merge_stub(conn, stub, post)
        assert conn.execute("SELECT COUNT(*) FROM items WHERE id = ?", (stub,)).fetchone()[0] == 0
        links = conn.execute(
            "SELECT from_id, to_id, kind FROM item_links ORDER BY from_id"
        ).fetchall()
        assert [tuple(r) for r in links] == [
            (comment, post, "in_reply_to"),
            (other, post, "references"),
        ]
        assert [r[0] for r in conn.execute("SELECT item_id FROM activities")] == [post]


def test_merging_never_deletes_a_held_item(test_db) -> None:
    with get_connection(test_db) as conn:
        held, post = _item(conn, "a"), _item(conn, "b")
        merge_stub(conn, held, post)
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
