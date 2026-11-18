"""Migration 36: one item per YouTube video (#368)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from lestash.core.database import SCHEMA_VERSION, apply_migrations, get_connection

VID = "DxL2HoqLbyA"
OTHER = "BgE6TA5iYaQ"


def _item(
    conn: sqlite3.Connection,
    source_id: str,
    content: str = "",
    metadata: dict[str, Any] | None = None,
    parent_id: int | None = None,
    source_type: str = "youtube",
    title: str | None = "A video",
) -> int:
    cursor = conn.execute(
        "INSERT INTO items (source_type, source_id, title, content, metadata, parent_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (source_type, source_id, title, content, json.dumps(metadata or {}), parent_id),
    )
    return int(cursor.lastrowid or 0)


def _migrate(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA user_version = 35")
    assert apply_migrations(conn) == SCHEMA_VERSION - 35


def _row(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT source_id, parent_id, title, content, metadata FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()


def _activities(conn: sqlite3.Connection, item_id: int) -> list[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT verb, source, occurred_at FROM activities WHERE item_id = ? ORDER BY verb",
        (item_id,),
    ).fetchall()
    return [tuple(r) for r in rows]


def test_a_liked_copy_folds_into_the_takeout_item_nested_under_it(test_db) -> None:
    with get_connection(test_db) as conn:
        liked = _item(
            conn,
            f"liked:{VID}",
            "The API description",
            {"source_subtype": "liked", "liked_at": "2026-01-20T14:00:00Z", "duration_seconds": 60},
        )
        video = _item(
            conn,
            f"history:{VID}",
            "A video\nChannel: Chan",
            {
                "source_subtype": "history",
                "source": "takeout",
                "channel_title": "Chan",
                "watched_count": 1,
                "_parent_source_id": f"liked:{VID}",
            },
            parent_id=liked,
        )
        conn.execute(
            "INSERT INTO activities (verb, item_id, occurred_at, source) "
            "VALUES ('watched', ?, '2026-01-21T10:00:00.000Z', 'takeout:youtube')",
            (video,),
        )
        transcript = _item(conn, f"transcript:{VID}", "words", parent_id=liked)
        note = _item(conn, "note-1", "my note", parent_id=liked, source_type="note")
        person = conn.execute(
            "INSERT INTO people (name, kind) VALUES ('Chan', 'channel')"
        ).lastrowid
        conn.execute(
            "INSERT INTO item_agents (item_id, person_id, role, origin) "
            "VALUES (?, ?, 'creator', 'test')",
            (liked, person),
        )
        tag = conn.execute("INSERT INTO tags (name) VALUES ('physics')").lastrowid
        conn.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (liked, tag))
        conn.execute(
            "INSERT INTO item_media (item_id, media_type, url) VALUES (?, 'thumbnail', 'u')",
            (liked,),
        )
        page = _item(conn, "https://example.com/p", "links the video", source_type="web")
        conn.execute(
            "INSERT INTO item_links (from_id, to_id, kind, origin, status) "
            "VALUES (?, ?, 'references', 'linker', 'confirmed')",
            (page, liked),
        )
        _migrate(conn)

        assert _row(conn, liked) is None
        row = _row(conn, video)
        assert row is not None
        assert row["source_id"] == f"history:{VID}"
        assert row["parent_id"] is None
        assert "The API description" in row["content"]
        assert "Watched once" in row["content"]
        metadata = json.loads(row["metadata"])
        assert metadata["description"] == "The API description"
        assert metadata["duration_seconds"] == 60
        assert metadata["source_subtype"] == "history"
        assert "_parent_source_id" not in metadata
        assert _activities(conn, video) == [
            ("liked", "youtube", "2026-01-20T14:00:00.000Z"),
            ("watched", "takeout:youtube", "2026-01-21T10:00:00.000Z"),
        ]
        for child in (transcript, note):
            assert (
                conn.execute("SELECT parent_id FROM items WHERE id = ?", (child,)).fetchone()[0]
                == video
            )
        for table in ("item_agents", "item_tags", "item_media"):
            count = conn.execute(
                f"SELECT count(*) FROM {table} WHERE item_id = ?", (video,)
            ).fetchone()[0]
            assert count == 1, table
        assert (
            conn.execute("SELECT to_id FROM item_links WHERE from_id = ?", (page,)).fetchone()[0]
            == video
        )
        reasons = {
            r[0]
            for r in conn.execute(
                "SELECT change_reason FROM item_history WHERE item_id = ?", (video,)
            )
        }
        assert reasons == {"migration 36"}


def test_a_like_takeout_already_records_is_not_added_twice(test_db) -> None:
    with get_connection(test_db) as conn:
        liked = _item(conn, f"liked:{VID}", "d", {"liked_at": "2026-01-20T14:00:03Z"})
        video = _item(conn, f"history:{VID}", "", {"source": "takeout"}, parent_id=liked)
        conn.execute(
            "INSERT INTO activities (verb, item_id, occurred_at, source) "
            "VALUES ('liked', ?, '2026-01-20T14:00:00.000Z', 'takeout:youtube')",
            (video,),
        )
        _migrate(conn)
        assert _activities(conn, video) == [
            ("liked", "takeout:youtube", "2026-01-20T14:00:00.000Z")
        ]


def test_a_share_and_a_like_without_takeout_become_the_older_item(test_db) -> None:
    with get_connection(test_db) as conn:
        shared = _item(conn, f"shared:{OTHER}", "desc", {"source_subtype": "shared"})
        note = _item(conn, "note-1", "my note", parent_id=shared, source_type="note")
        liked = _item(conn, f"liked:{OTHER}", "desc", {"liked_at": "2026-11-18T00:57:11Z"})
        lone = _item(conn, f"liked:{VID}", "desc", {"liked_at": "2026-01-01T00:00:00Z"})
        _migrate(conn)

        assert _row(conn, liked) is None
        row = _row(conn, shared)
        assert row is not None
        assert row["source_id"] == f"history:{OTHER}"
        assert row["content"] == "desc"
        assert json.loads(row["metadata"])["source_subtype"] == "history"
        assert (
            conn.execute("SELECT parent_id FROM items WHERE id = ?", (note,)).fetchone()[0]
            == shared
        )
        assert _activities(conn, shared) == [("liked", "youtube", "2026-11-18T00:57:11.000Z")]

        lone_row = _row(conn, lone)
        assert lone_row is not None
        assert lone_row["source_id"] == f"history:{VID}"
        assert _activities(conn, lone) == [("liked", "youtube", "2026-01-01T00:00:00.000Z")]


def test_a_real_title_replaces_one_takeout_lost(test_db) -> None:
    with get_connection(test_db) as conn:
        liked = _item(conn, f"liked:{VID}", "desc", title="The real title")
        video = _item(
            conn,
            f"history:{VID}",
            "(video no longer available)",
            {"source": "takeout", "unavailable": True},
            parent_id=liked,
            title="(video no longer available)",
        )
        _migrate(conn)
        row = _row(conn, video)
        assert row is not None
        assert row["title"] == "The real title"
        assert row["content"].startswith("The real title")
        assert "unavailable" not in json.loads(row["metadata"])


def test_a_database_without_copies_is_left_alone(test_db) -> None:
    with get_connection(test_db) as conn:
        video = _item(conn, f"history:{VID}", "kept as is", {"source": "takeout"})
        before = _row(conn, video)
        assert before is not None
        before_values = tuple(before)
        _migrate(conn)
        after = _row(conn, video)
        assert after is not None
        assert tuple(after) == before_values
        assert conn.execute("SELECT count(*) FROM item_history").fetchone()[0] == 0


def test_a_like_two_writers_know_of_is_kept_once(test_db) -> None:
    """Takeout and the API sync each record a like, seconds apart: one stays."""
    from datetime import UTC, datetime

    from lestash.core.activities import record_activities
    from lestash.importers import merge_with_stored
    from lestash.models.item import ActivityCreate, ItemCreate

    def like(source: str, second: int) -> ActivityCreate:
        return ActivityCreate(
            verb="liked",
            occurred_at=datetime(2026, 1, 20, 14, 0, second, tzinfo=UTC),
            source=source,
        )

    with get_connection(test_db) as conn:
        video = _item(conn, f"history:{VID}", "", {"source": "takeout"})
        record_activities(conn, video, [like("takeout:youtube", 0)])
        item = ItemCreate(
            source_type="youtube",
            source_id=f"history:{VID}",
            content="desc",
            activities=[like("youtube", 3)],
        )
        assert merge_with_stored(conn, item)
        assert item.activities == []
        again = ItemCreate(
            source_type="youtube",
            source_id=f"history:{VID}",
            content="",
            metadata={"source": "takeout"},
            activities=[like("takeout:youtube", 0)],
        )
        assert merge_with_stored(conn, again)
        assert again.activities == [like("takeout:youtube", 0)]  # its own: stored once by key
        later = ItemCreate(
            source_type="youtube",
            source_id=f"history:{VID}",
            content="desc",
            activities=[
                ActivityCreate(
                    verb="liked", occurred_at=datetime(2026, 3, 1, tzinfo=UTC), source="youtube"
                )
            ],
        )
        assert merge_with_stored(conn, later)
        assert len(later.activities or []) == 1  # liked again, weeks later: another like


def test_a_like_at_another_time_than_takeouts_is_kept(test_db) -> None:
    with get_connection(test_db) as conn:
        liked = _item(conn, f"liked:{VID}", "d", {"liked_at": "2026-03-01T00:00:00Z"})
        video = _item(conn, f"history:{VID}", "", {"source": "takeout"}, parent_id=liked)
        conn.execute(
            "INSERT INTO activities (verb, item_id, occurred_at, source) "
            "VALUES ('liked', ?, '2026-01-20T14:00:00.000Z', 'takeout:youtube')",
            (video,),
        )
        _migrate(conn)
        assert _activities(conn, video) == [
            ("liked", "takeout:youtube", "2026-01-20T14:00:00.000Z"),
            ("liked", "youtube", "2026-03-01T00:00:00.000Z"),
        ]


def test_a_copy_added_under_an_item_keeps_that_place(test_db) -> None:
    with get_connection(test_db) as conn:
        page = _item(conn, "note-9", "a note", source_type="note")
        video = _item(conn, f"history:{VID}", "", {"source": "takeout"})
        _item(conn, f"shared:{VID}", "desc", parent_id=page)
        _migrate(conn)
        row = _row(conn, video)
        assert row is not None
        assert row["parent_id"] == page


def test_stale_markers_go_from_every_video_and_child(test_db) -> None:
    with get_connection(test_db) as conn:
        alone = _item(
            conn,
            f"history:{OTHER}",
            "",
            {"source": "takeout", "_parent_source_id": f"liked:{OTHER}"},
        )
        transcript = _item(
            conn, f"transcript:{OTHER}", "words", {"_parent_source_id": f"liked:{OTHER}"}
        )
        _migrate(conn)
        alone_row, transcript_row = _row(conn, alone), _row(conn, transcript)
        assert alone_row is not None and transcript_row is not None
        assert "_parent_source_id" not in json.loads(alone_row["metadata"])
        assert json.loads(transcript_row["metadata"])["_parent_source_id"] == f"history:{OTHER}"


def test_mentions_move_with_the_copy(test_db) -> None:
    with get_connection(test_db) as conn:
        liked = _item(conn, f"liked:{VID}", "d")
        video = _item(conn, f"history:{VID}", "", {"source": "takeout"}, parent_id=liked)
        person = conn.execute("INSERT INTO people (name, kind) VALUES ('Ann', 'person')").lastrowid
        conn.execute(
            "INSERT INTO item_mentions (item_id, person_id, status, origin) "
            "VALUES (?, ?, 'confirmed', 'test')",
            (liked, person),
        )
        _migrate(conn)
        assert (
            conn.execute(
                "SELECT item_id FROM item_mentions WHERE person_id = ?", (person,)
            ).fetchone()[0]
            == video
        )


def test_a_status_decided_on_the_copy_survives(test_db) -> None:
    with get_connection(test_db) as conn:
        liked = _item(conn, f"liked:{VID}", "d")
        video = _item(conn, f"history:{VID}", "", {"source": "takeout"}, parent_id=liked)
        person = conn.execute("INSERT INTO people (name, kind) VALUES ('Ann', 'person')").lastrowid
        conn.executemany(
            "INSERT INTO item_mentions (item_id, person_id, status, origin) "
            "VALUES (?, ?, ?, 'test')",
            [(liked, person, "rejected"), (video, person, "suggested")],
        )
        _migrate(conn)
        rows = conn.execute("SELECT item_id, status FROM item_mentions").fetchall()
        assert [tuple(r) for r in rows] == [(video, "rejected")]


def test_a_copy_nested_under_another_copy_leaves_no_dangling_parent(test_db) -> None:
    with get_connection(test_db) as conn:
        page = _item(conn, "note-9", "a note", source_type="note")
        shared = _item(conn, f"shared:{VID}", "desc", parent_id=page)
        _item(conn, f"liked:{VID}", "desc", parent_id=shared)
        video = _item(conn, f"history:{VID}", "", {"source": "takeout"}, parent_id=shared)
        _migrate(conn)
        assert (
            conn.execute("SELECT count(*) FROM items WHERE source_type = 'youtube'").fetchone()[0]
            == 1
        )
        row = _row(conn, video)
        assert row is not None
        assert row["parent_id"] == page


def test_a_copy_under_one_of_the_items_own_parts_makes_no_loop(test_db) -> None:
    with get_connection(test_db) as conn:
        video = _item(conn, f"history:{VID}", "", {"source": "takeout"})
        note = _item(conn, "note-9", "a note", parent_id=video, source_type="note")
        _item(conn, f"shared:{VID}", "desc", parent_id=note)
        _migrate(conn)
        row = _row(conn, video)
        assert row is not None
        assert row["parent_id"] is None
        assert (
            conn.execute("SELECT parent_id FROM items WHERE id = ?", (note,)).fetchone()[0] == video
        )


def test_an_item_under_a_note_under_a_copy_takes_the_copys_place(test_db) -> None:
    with get_connection(test_db) as conn:
        page = _item(conn, "note-1", "a page", source_type="note")
        liked = _item(conn, f"liked:{VID}", "d", parent_id=page)
        note = _item(conn, "note-2", "a note", parent_id=liked, source_type="note")
        video = _item(conn, f"history:{VID}", "", {"source": "takeout"}, parent_id=note)
        _migrate(conn)
        parents = dict(conn.execute("SELECT id, parent_id FROM items").fetchall())
        assert parents[video] == page
        assert parents[note] == video


def test_a_place_under_a_copy_not_yet_folded_makes_no_loop(test_db) -> None:
    with get_connection(test_db) as conn:
        page = _item(conn, "note-1", "a page", source_type="note")
        video = _item(conn, f"history:{VID}", "", {"source": "takeout"})
        outer = _item(conn, f"shared:{VID}", "d", parent_id=page)
        note = _item(conn, "note-2", "a note", parent_id=outer, source_type="note")
        _item(conn, f"liked:{VID}", "d", parent_id=note)
        _migrate(conn)
        parents = dict(conn.execute("SELECT id, parent_id FROM items").fetchall())
        assert parents[video] == page
        assert parents[note] == video
