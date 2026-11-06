"""Children that name their parent via metadata._parent_source_id get linked on sync."""

from __future__ import annotations

from collections.abc import Iterator

from lestash.cli import sources as sources_cli
from lestash.core.database import (
    get_connection,
    resolve_parent_id,
    resolve_parent_source_ids,
    upsert_item,
)
from lestash.models.item import ItemCreate


def _book() -> ItemCreate:
    return ItemCreate(source_type="audible", source_id="audible:book:B1", content="A book")


def _note(n: int) -> ItemCreate:
    return ItemCreate(
        source_type="audible",
        source_id=f"audible:annotation:B1:{n}",
        content=f"note {n}",
        metadata={"_parent_source_id": "audible:book:B1"},
    )


def _parent_of(conn, source_id: str) -> int | None:
    return conn.execute("SELECT parent_id FROM items WHERE source_id = ?", (source_id,)).fetchone()[
        0
    ]


def test_resolve_parent_id_links_to_existing_parent(test_db) -> None:
    with get_connection(test_db) as conn:
        book_id = upsert_item(conn, _book())
        note = _note(1)
        resolve_parent_id(conn, note)
        assert note.parent_id == book_id


def test_resolve_parent_id_keeps_explicit_parent_and_ignores_missing(test_db) -> None:
    with get_connection(test_db) as conn:
        upsert_item(conn, _book())
        explicit = _note(1)
        explicit.parent_id = 999
        resolve_parent_id(conn, explicit)
        assert explicit.parent_id == 999

        orphan = _note(2)
        orphan.metadata = {"_parent_source_id": "audible:book:missing"}
        resolve_parent_id(conn, orphan)
        assert orphan.parent_id is None


def test_resolve_parent_source_ids_links_children_saved_before_parent(test_db) -> None:
    with get_connection(test_db) as conn:
        upsert_item(conn, _note(1))
        upsert_item(conn, _note(2))
        # Same source_id in another source must not be used as the parent
        upsert_item(conn, ItemCreate(source_type="other", source_id="audible:book:B1", content="x"))
        book_id = upsert_item(conn, _book())

        assert resolve_parent_source_ids(conn, "audible") == 2
        assert _parent_of(conn, "audible:annotation:B1:1") == book_id
        assert _parent_of(conn, "audible:annotation:B1:2") == book_id
        # Idempotent
        assert resolve_parent_source_ids(conn, "audible") == 0


def test_resolve_parent_source_ids_does_not_override_existing_parent(test_db) -> None:
    with get_connection(test_db) as conn:
        upsert_item(conn, _book())
        other_id = upsert_item(conn, ItemCreate(source_type="audible", source_id="x", content="x"))
        note = _note(1)
        note.parent_id = other_id
        upsert_item(conn, note)

        assert resolve_parent_source_ids(conn) == 0
        assert _parent_of(conn, "audible:annotation:B1:1") == other_id


class _FakePlugin:
    name = "audible"

    def sync(self, config: dict) -> Iterator[ItemCreate]:
        yield _book()
        yield _note(1)
        yield _note(2)


def test_sources_sync_links_children(test_db, monkeypatch) -> None:
    monkeypatch.setattr(sources_cli, "load_plugins", lambda: {"audible": _FakePlugin()})
    monkeypatch.setattr(sources_cli.Config, "load", classmethod(lambda cls: test_db))

    # Run twice: a re-sync must keep the links
    sources_cli.sync_source(source_name="audible", all_sources=False)
    sources_cli.sync_source(source_name="audible", all_sources=False)

    with get_connection(test_db) as conn:
        book_id = conn.execute(
            "SELECT id FROM items WHERE source_id = 'audible:book:B1'"
        ).fetchone()[0]
        assert _parent_of(conn, "audible:annotation:B1:1") == book_id
        assert _parent_of(conn, "audible:annotation:B1:2") == book_id
