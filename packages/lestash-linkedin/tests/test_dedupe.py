"""LinkedIn items held twice under an old and a current source id (#244)."""

import json
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import get_connection, init_database
from lestash_linkedin.dedupe import dedupe_linkedin, find_duplicates


@pytest.fixture
def test_db() -> Iterator[Config]:
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config(general=GeneralConfig(database_path=str(Path(tmpdir) / "t.db")))
        init_database(config)
        yield config


def _item(conn, source_id: str, event_id: int, resource_id: str = "7441", **extra) -> int:
    metadata = {
        "resource_name": "ugcPosts",
        "raw": {"id": event_id, "resourceId": resource_id},
        **extra,
    }
    return conn.execute(
        "INSERT INTO items (source_type, source_id, content, metadata) "
        "VALUES ('linkedin', ?, 'A post', ?) RETURNING id",
        (source_id, json.dumps(metadata)),
    ).fetchone()[0]


def test_the_same_event_twice_is_merged_into_the_current_copy(test_db) -> None:
    with get_connection(test_db) as conn:
        old = _item(conn, "changelog-ugcPosts-1774078761862", 1)
        current = _item(conn, "changelog-ugcPosts-7441", 1)
        note = conn.execute(
            "INSERT INTO items (source_type, content, parent_id) VALUES ('note', 'n', ?) "
            "RETURNING id",
            (old,),
        ).fetchone()[0]
        tag = conn.execute("INSERT INTO tags (name) VALUES ('t') RETURNING id").fetchone()[0]
        conn.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (old, tag))
        conn.execute(
            "INSERT INTO item_media (item_id, media_type, url) VALUES (?, 'image', 'u')", (old,)
        )
        # A different event for the same resource, such as an edit, is kept
        edit = _item(conn, "changelog-ugcPosts-1774078799999", 2)
        assert [(p.old_id, p.current_id) for p in find_duplicates(conn)] == [(old, current)]
        assert len(dedupe_linkedin(conn)) == 1
        ids = {r[0] for r in conn.execute("SELECT id FROM items")}
        assert ids == {current, note, edit}
        assert conn.execute("SELECT parent_id FROM items WHERE id = ?", (note,)).fetchone()[0] == (
            current
        )
        assert conn.execute("SELECT item_id FROM item_tags").fetchone()[0] == current
        assert conn.execute("SELECT item_id FROM item_media").fetchone()[0] == current
        assert dedupe_linkedin(conn) == []


def test_media_are_not_added_twice(test_db) -> None:
    with get_connection(test_db) as conn:
        old = _item(conn, "changelog-ugcPosts-1774078761862", 1)
        current = _item(conn, "changelog-ugcPosts-7441", 1)
        for item in (old, current):
            conn.execute(
                "INSERT INTO item_media (item_id, media_type, url) VALUES (?, 'image', 'u')",
                (item,),
            )
        dedupe_linkedin(conn)
        assert [tuple(r) for r in conn.execute("SELECT item_id FROM item_media")] == [(current,)]
