"""Activities: imports attach the times to their items, and the writers store them."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone

from lestash.core.activities import activity_time, record_activities
from lestash.core.database import apply_migrations, get_connection
from lestash.core.sync import SyncStore
from lestash.importers.youtube_history import parse_search_history, parse_watch_history
from lestash.models.item import ActivityCreate


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


def _history_rows(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM item_history").fetchone()[0]


def test_an_import_records_each_time_once_and_a_re_import_adds_only_new_ones(test_db) -> None:
    first = [_watch("AAAAAAAAAAA", "2025-06-01T10:00:00Z")]
    with get_connection(test_db) as conn:
        SyncStore(conn, "youtube").run(iter(parse_watch_history(first)))
        # The same export again writes nothing: no item change, no history row
        history = _history_rows(conn)
        SyncStore(conn, "youtube").run(iter(parse_watch_history(first)))
        assert _history_rows(conn) == history
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
        metadata, content = conn.execute("SELECT metadata, content FROM items").fetchone()
    metadata = json.loads(metadata)
    assert "watched_times" not in metadata
    assert (metadata["watched_count"], metadata["first_at"], metadata["last_at"]) == (
        2,
        "2025-06-01T10:00:00+00:00",
        "2026-01-22T14:36:35.783000+00:00",
    )
    assert "Watched 2 times: 2026-01-22, 2025-06-01" in content


def test_an_older_export_keeps_the_later_times_already_stored(test_db) -> None:
    with get_connection(test_db) as conn:
        SyncStore(conn, "youtube").run(
            iter(parse_watch_history([_watch("AAAAAAAAAAA", "2026-01-22T14:36:35.783Z")]))
        )
        SyncStore(conn, "youtube").run(
            iter(parse_watch_history([_watch("AAAAAAAAAAA", "2025-06-01T10:00:00Z")]))
        )
        metadata = json.loads(conn.execute("SELECT metadata FROM items").fetchone()[0])
        assert len(_activities(conn)) == 2
    assert (metadata["watched_count"], metadata["first_at"], metadata["last_at"]) == (
        2,
        "2025-06-01T10:00:00+00:00",
        "2026-01-22T14:36:35.783000+00:00",
    )


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


def test_times_are_stored_in_utc_to_the_millisecond() -> None:
    paris = timezone(timedelta(hours=1))
    assert activity_time(datetime(2026, 11, 5, 15, 11, 6, 941999, paris)) == (
        "2026-11-05T14:11:06.941Z"
    )
    assert activity_time(datetime(2026, 11, 5, 15, 11, 6)) == "2026-11-05T15:11:06.000Z"


def test_a_time_stored_twice_is_one_activity_and_a_deleted_item_takes_its_own(
    test_db,
) -> None:
    when = datetime(2026, 1, 1, tzinfo=UTC)
    watch = ActivityCreate(verb="watched", occurred_at=when, source="takeout:youtube")
    with get_connection(test_db) as conn:
        item_id = conn.execute(
            "INSERT INTO items (source_type, source_id, content) "
            "VALUES ('youtube', 'history:B', 'x') RETURNING id"
        ).fetchone()[0]
        assert record_activities(conn, item_id, [watch, watch]) == 1
        assert record_activities(conn, item_id, [watch]) == 0
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0


def _store_raw(conn: sqlite3.Connection, rows: list[tuple[str, str]]) -> None:
    conn.executemany(
        "INSERT INTO items (source_type, source_id, content, metadata) "
        "VALUES ('youtube', ?, 'x', ?)",
        rows,
    )
    conn.commit()


def test_the_migrations_move_the_lists_already_stored_into_the_table(test_db) -> None:
    with get_connection(test_db) as conn:
        # Back to schema 13, with items stored before the table existed
        conn.executescript("DROP TABLE activities; PRAGMA user_version = 13;")
        _store_raw(
            conn,
            [
                (
                    "history:A",
                    json.dumps(
                        {
                            "watched_times": [
                                "2025-06-01T10:00:00+00:00",
                                "2026-01-22T14:36:35.783000+00:00",
                            ],
                            "watched_count": 2,
                        }
                    ),
                ),
                ("post:P", json.dumps({"viewed_times": ["2026-01-20T18:48:57+00:00"]})),
                ("bad", "{not json"),
            ],
        )
        assert apply_migrations(conn) == 3  # 14, 15 and 16
        assert [(r[0], r[1], r[2]) for r in _activities(conn)] == [
            ("watched", "history:A", "2025-06-01T10:00:00.000Z"),
            ("viewed", "post:P", "2026-01-20T18:48:57.000Z"),
            ("watched", "history:A", "2026-01-22T14:36:35.783Z"),
        ]
        rows = dict(conn.execute("SELECT source_id, metadata FROM items"))
        assert json.loads(rows["history:A"]) == {"watched_count": 2}
        assert rows["post:P"] is None  # as writers store empty metadata
        assert rows["bad"] == "{not json"
        history = conn.execute(
            "SELECT i.source_id, h.change_reason, h.metadata_old "
            "FROM item_history h JOIN items i ON i.id = h.item_id ORDER BY i.source_id"
        ).fetchall()
        assert [(r[0], r[1]) for r in history] == [
            ("history:A", "migration 16: times lists moved to activities"),
            ("post:P", "migration 16: times lists moved to activities"),
        ]
        assert "watched_times" in history[0][2]  # the old metadata is kept
        triggers = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE '%activities%'"
        ).fetchall()
        assert triggers == []


def test_migration_16_records_listed_times_not_yet_recorded_but_no_non_dates(test_db) -> None:
    """strftime would read "now", "10:00" or a number as a time; none is recorded."""
    with get_connection(test_db) as conn:
        conn.execute("PRAGMA user_version = 15")
        times = ["now", "10:00", "100", "2026-11-05T15:11:06.941+00:00"]
        _store_raw(conn, [("history:C", json.dumps({"watched_times": times}))])
        assert apply_migrations(conn) == 1
        assert [r[2] for r in _activities(conn)] == ["2026-11-05T15:11:06.941Z"]
        assert conn.execute("SELECT metadata FROM items").fetchone()[0] is None


def test_after_migration_16_the_same_export_again_writes_nothing(test_db) -> None:
    """The migration keeps the metadata's format, so the upsert finds it unchanged."""
    export = [
        _watch("AAAAAAAAAAA", "2025-06-01T10:00:00Z"),
        _watch("AAAAAAAAAAA", "2026-01-22T14:36:35.783Z"),
    ]
    [item] = parse_watch_history(export)
    old_metadata = {
        **(item.metadata or {}),
        "watched_times": [a.occurred_at.isoformat() for a in item.activities or []],
    }
    with get_connection(test_db) as conn:
        conn.execute("PRAGMA user_version = 15")
        conn.execute(
            "INSERT INTO items (source_type, source_id, url, title, content, author, "
            "created_at, is_own_content, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.source_type,
                item.source_id,
                item.url,
                item.title,
                item.content,
                item.author,
                item.created_at,
                item.is_own_content,
                json.dumps(old_metadata),
            ),
        )
        conn.commit()
        assert apply_migrations(conn) == 1
        assert len(_activities(conn)) == 2
        history = _history_rows(conn)
        store = SyncStore(conn, "youtube")
        store.run(iter(parse_watch_history(export)))
        assert _history_rows(conn) == history
        assert (store.counts.updated, store.counts.unchanged) == (0, 1)


def test_a_later_like_keeps_the_watches_already_stored(test_db) -> None:
    like = {
        "header": "YouTube",
        "title": "Liked A talk",
        "titleUrl": "https://www.youtube.com/watch?v=AAAAAAAAAAA",
        "time": "2026-02-01T10:00:00Z",
    }
    with get_connection(test_db) as conn:
        SyncStore(conn, "youtube").run(
            iter(parse_watch_history([_watch("AAAAAAAAAAA", "2026-01-01T10:00:00Z")]))
        )
        SyncStore(conn, "youtube").run(iter(parse_watch_history([like])))
        assert [r[0] for r in _activities(conn)] == ["watched", "liked"]
        metadata, content = conn.execute("SELECT metadata, content FROM items").fetchone()
    assert json.loads(metadata)["watched_count"] == 1
    assert "Watched once: 2026-01-01" in content
