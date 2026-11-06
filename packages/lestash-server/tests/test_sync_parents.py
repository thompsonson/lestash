"""Server-triggered syncs link children to parents via metadata._parent_source_id."""

from __future__ import annotations

import lestash_server.deps as deps
import lestash_server.routes.sources as sources_route
from lestash.core.database import get_connection
from lestash.models.item import ItemCreate


class _FakePlugin:
    def sync(self, config: dict):
        yield ItemCreate(source_type="audible", source_id="audible:book:B1", content="A book")
        yield ItemCreate(
            source_type="audible",
            source_id="audible:annotation:B1:1",
            content="a note",
            metadata={"_parent_source_id": "audible:book:B1"},
        )


def test_run_sync_links_children(test_config, monkeypatch) -> None:
    monkeypatch.setattr(deps, "_config", test_config)
    monkeypatch.setattr(sources_route, "load_plugins", lambda: {"audible": _FakePlugin()})

    sources_route._run_sync("audible")
    sources_route._run_sync("audible")  # re-sync keeps the link

    with get_connection(test_config) as conn:
        book_id, note_parent = conn.execute(
            """
            SELECT b.id, n.parent_id FROM items b, items n
            WHERE b.source_id = 'audible:book:B1' AND n.source_id = 'audible:annotation:B1:1'
            """
        ).fetchone()
    assert note_parent == book_id
