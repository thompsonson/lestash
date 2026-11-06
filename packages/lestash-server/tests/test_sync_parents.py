"""Server-triggered syncs link children to parents via metadata._parent_source_id."""

from __future__ import annotations

import lestash_server.deps as deps
import lestash_server.routes.sources as sources_route
from lestash.core.database import get_connection
from lestash.models.item import ItemCreate

BOOK = ItemCreate(source_type="audible", source_id="audible:book:B1", content="A book")
NOTE = ItemCreate(
    source_type="audible",
    source_id="audible:annotation:B1:1",
    content="a note",
    metadata={"_parent_source_id": "audible:book:B1"},
)


class _FakePlugin:
    def __init__(self, *items: ItemCreate) -> None:
        self.items = items

    def sync(self, config: dict):
        # Fresh copies: resolve_parent_id mutates the item
        for item in self.items:
            yield item.model_copy(deep=True)


def _ids(test_config) -> tuple[int, int | None]:
    with get_connection(test_config) as conn:
        return conn.execute(
            """
            SELECT b.id, n.parent_id FROM items b, items n
            WHERE b.source_id = 'audible:book:B1' AND n.source_id = 'audible:annotation:B1:1'
            """
        ).fetchone()


def test_run_sync_links_children(test_config, monkeypatch) -> None:
    monkeypatch.setattr(deps, "_config", test_config)
    monkeypatch.setattr(sources_route, "load_plugins", lambda: {"audible": _FakePlugin(BOOK, NOTE)})

    sources_route._run_sync("audible")
    sources_route._run_sync("audible")  # re-sync keeps the link

    book_id, note_parent = _ids(test_config)
    assert note_parent == book_id


def test_run_sync_links_child_yielded_before_parent(test_config, monkeypatch) -> None:
    monkeypatch.setattr(deps, "_config", test_config)
    monkeypatch.setattr(sources_route, "load_plugins", lambda: {"audible": _FakePlugin(NOTE, BOOK)})

    sources_route._run_sync("audible")

    book_id, note_parent = _ids(test_config)
    assert note_parent == book_id


def test_run_sync_keeps_a_parent_set_by_a_resolver(test_config, monkeypatch) -> None:
    reaction = ItemCreate(source_type="audible", source_id="reaction:1", content="liked")
    monkeypatch.setattr(deps, "_config", test_config)
    monkeypatch.setattr(
        sources_route, "load_plugins", lambda: {"audible": _FakePlugin(BOOK, reaction)}
    )

    sources_route._run_sync("audible")
    with get_connection(test_config) as conn:
        book_id = conn.execute(
            "SELECT id FROM items WHERE source_id = 'audible:book:B1'"
        ).fetchone()[0]
        conn.execute("UPDATE items SET parent_id = ? WHERE source_id = 'reaction:1'", (book_id,))
        conn.commit()

    sources_route._run_sync("audible")

    with get_connection(test_config) as conn:
        parent = conn.execute(
            "SELECT parent_id FROM items WHERE source_id = 'reaction:1'"
        ).fetchone()[0]
    assert parent == book_id
