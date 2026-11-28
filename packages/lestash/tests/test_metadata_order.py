"""Item metadata is stored with sorted keys, so key order alone is never a change (#441)."""

import json

from lestash.core.database import (
    SCHEMA_VERSION,
    apply_migrations,
    dump_metadata,
    get_connection,
    upsert_item,
)
from lestash.models.item import ItemCreate


def _history(conn, item_id: int) -> int:
    return conn.execute(
        "SELECT count(*) FROM item_history WHERE item_id = ?", (item_id,)
    ).fetchone()[0]


def test_the_same_metadata_in_another_order_is_no_change(test_db) -> None:
    def item(metadata: dict) -> ItemCreate:
        return ItemCreate(source_type="note", source_id="n1", content="x", metadata=metadata)

    with get_connection(test_db) as conn:
        item_id = upsert_item(conn, item({"sender": "human", "files": ["a.png"]}))
        upsert_item(conn, item({"files": ["a.png"], "sender": "human"}))
        assert _history(conn, item_id) == 0
        stored = conn.execute("SELECT metadata FROM items WHERE id = ?", (item_id,)).fetchone()[0]
        assert stored == '{"files": ["a.png"], "sender": "human"}'


def test_nested_keys_are_sorted_too() -> None:
    assert dump_metadata({"b": {"z": 1, "a": 2}, "a": [{"y": 1, "x": 2}]}) == (
        '{"a": [{"x": 2, "y": 1}], "b": {"a": 2, "z": 1}}'
    )


def test_migration_41_sorts_stored_metadata_and_writes_no_history(test_db) -> None:
    with get_connection(test_db) as conn:
        rows = {
            "unsorted": '{"sender": "human", "attachments": [{"file_size": 1, "file_name": "a"}]}',
            "minified": '{"b":1,"a":2}',
            "sorted": '{"a": 1}',
            "not json": "not json",
        }
        ids = {}
        for name, text in rows.items():
            ids[name] = conn.execute(
                "INSERT INTO items (source_type, source_id, content, metadata)"
                " VALUES (?, ?, 'x', ?)",
                ("note", name, text),
            ).lastrowid
        conn.execute("PRAGMA user_version = 40")
        assert apply_migrations(conn) == SCHEMA_VERSION - 40

        stored = {
            name: conn.execute("SELECT metadata FROM items WHERE id = ?", (i,)).fetchone()[0]
            for name, i in ids.items()
        }
        assert stored["unsorted"] == (
            '{"attachments": [{"file_name": "a", "file_size": 1}], "sender": "human"}'
        )
        assert stored["minified"] == '{"a": 2, "b": 1}'
        assert stored["sorted"] == '{"a": 1}'
        assert stored["not json"] == "not json"
        assert json.loads(stored["unsorted"]) == json.loads(rows["unsorted"])
        assert conn.execute("SELECT count(*) FROM item_history").fetchone()[0] == 0
        # The search index still matches: its trigger now fires only for indexed columns
        conn.execute("INSERT INTO items_fts(items_fts) VALUES('integrity-check')")
        conn.execute("UPDATE items SET content = 'zebra' WHERE id = ?", (ids["sorted"],))
        found = conn.execute("SELECT rowid FROM items_fts WHERE items_fts MATCH 'zebra'")
        assert [r[0] for r in found] == [ids["sorted"]]
