"""The activities table, filled from the times lists that imports keep in item metadata."""

from __future__ import annotations

import json
import sqlite3

from lestash.core.database import apply_migrations, get_connection
from lestash.core.sync import SyncStore
from lestash.importers.youtube_history import parse_search_history, parse_watch_history
from lestash.models.item import ItemCreate


def _watch(video_id: str, when: str) -> dict:
    return {
        "header": "YouTube",
        "title": "Watched A talk",
        "titleUrl": f"https://www.youtube.com/watch?v={video_id}",
        "time": when,
        "products": ["YouTube"],
    }


def _activities(conn: sqlite3.Connection) -> list[tuple]:
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT verb, i.source_id, occurred_at, a.source, details "
            "FROM activities a JOIN items i ON i.id = a.item_id ORDER BY occurred_at, verb"
        )
    ]


def test_an_import_records_each_time_once_and_a_re_import_adds_only_new_ones(test_db) -> None:
    with get_connection(test_db) as conn:
        SyncStore(conn, "youtube").run(
            iter(parse_watch_history([_watch("AAAAAAAAAAA", "2025-06-01T10:00:00Z")]))
        )
        # A later export holds the same watch, in another precision, and a new one
        SyncStore(conn, "youtube").run(
            iter(
                parse_watch_history(
                    [
                        _watch("AAAAAAAAAAA", "2025-06-01T10:00:00.000Z"),
                        _watch("AAAAAAAAAAA", "2026-01-22T14:36:35.783Z"),
                    ]
                )
            )
        )
        assert _activities(conn) == [
            ("watched", "history:AAAAAAAAAAA", "2025-06-01T10:00:00.000Z", "takeout:youtube", None),
            ("watched", "history:AAAAAAAAAAA", "2026-01-22T14:36:35.783Z", "takeout:youtube", None),
        ]


def test_a_search_keeps_its_query(test_db) -> None:
    search = {
        "header": "YouTube",
        "title": "Searched for markov blanket",
        "titleUrl": "https://www.youtube.com/results?search_query=markov+blanket",
        "time": "2026-01-17T23:20:58.043Z",
        "products": ["YouTube"],
    }
    with get_connection(test_db) as conn:
        SyncStore(conn, "youtube").run(iter(parse_search_history([search])))
        [(verb, _, occurred_at, _, details)] = _activities(conn)
    assert (verb, occurred_at) == ("searched", "2026-01-17T23:20:58.043Z")
    assert json.loads(details) == {"query": "markov blanket"}


def test_times_are_stored_in_utc_and_bad_values_are_skipped(test_db) -> None:
    item = ItemCreate(
        source_type="gemini",
        source_id="c_1",
        content="x",
        metadata={"prompted_times": ["2026-11-05T15:11:06.941+01:00", "not a time", 7]},
    )
    with get_connection(test_db) as conn:
        SyncStore(conn, "gemini").run(iter([item]))
        assert _activities(conn) == [
            ("prompted", "c_1", "2026-11-05T14:11:06.941Z", "takeout:gemini", None)
        ]


def test_other_metadata_records_nothing_and_a_deleted_item_takes_its_activities(
    test_db,
) -> None:
    items = [
        ItemCreate(
            source_type="youtube",
            source_id="history:B",
            content="x",
            metadata={"watched_times": ["2026-01-01T00:00:00+00:00"]},
        ),
        # A string, not a list; invalid JSON can't reach here through ItemCreate
        ItemCreate(
            source_type="youtube",
            source_id="history:C",
            content="x",
            metadata={"watched_times": "2026-01-01T00:00:00+00:00"},
        ),
        ItemCreate(source_type="bluesky", source_id="p", content="x", metadata={"a": 1}),
    ]
    with get_connection(test_db) as conn:
        SyncStore(conn, "youtube").run(iter(items))
        assert [r[1] for r in _activities(conn)] == ["history:B"]
        conn.execute("DELETE FROM items WHERE source_id = 'history:B'")
        assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0


def test_the_migration_fills_the_table_from_items_already_stored(test_db) -> None:
    with get_connection(test_db) as conn:
        # Back to schema 13, with items stored before the table existed
        conn.executescript(
            "DROP TRIGGER items_activities_ai; DROP TRIGGER items_activities_au;"
            "DROP TABLE activities; PRAGMA user_version = 13;"
        )
        conn.executemany(
            "INSERT INTO items (source_type, source_id, content, metadata) VALUES (?, ?, 'x', ?)",
            [
                (
                    "youtube",
                    "history:A",
                    json.dumps(
                        {
                            "watched_times": [
                                "2025-06-01T10:00:00+00:00",
                                "2026-01-22T14:36:35.783000+00:00",
                            ]
                        }
                    ),
                ),
                ("youtube", "post:P", json.dumps({"viewed_times": ["2026-01-20T18:48:57+00:00"]})),
                ("youtube", "bad", "{not json"),
            ],
        )
        conn.commit()
        assert apply_migrations(conn) == 2  # 14 and 15
        assert [(r[0], r[1], r[2]) for r in _activities(conn)] == [
            ("watched", "history:A", "2025-06-01T10:00:00.000Z"),
            ("viewed", "post:P", "2026-01-20T18:48:57.000Z"),
            ("watched", "history:A", "2026-01-22T14:36:35.783Z"),
        ]
