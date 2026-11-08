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


class _CheckpointThenFail:
    """Zip 1 (items, then its checkpoint), then zip 2 fails after a child and its parent."""

    def sync(self, config: dict):
        from lestash.models.sync import SyncCheckpoint

        yield ItemCreate(source_type="audible", source_id="other", content="zip 1")
        yield SyncCheckpoint(key="zip1", value={"status": "imported"})
        yield NOTE.model_copy(deep=True)
        yield BOOK.model_copy(deep=True)
        raise RuntimeError("zip 2 failed")


def test_run_sync_commits_checkpoints_with_their_items(test_config, monkeypatch) -> None:
    from lestash.core.database import get_checkpoints

    monkeypatch.setattr(deps, "_config", test_config)
    monkeypatch.setattr(sources_route, "load_plugins", lambda: {"audible": _CheckpointThenFail()})

    sources_route._run_sync("audible")

    with get_connection(test_config) as conn:
        assert get_checkpoints(conn, "audible") == {"zip1": {"status": "imported"}}
        status, added, error = conn.execute(
            "SELECT status, items_added, error_message FROM sync_log WHERE source_type = 'audible'"
        ).fetchone()
    assert (status, added, error) == ("failed", 3, "zip 2 failed")
    # What the failed sync stored is still linked
    book_id, note_parent = _ids(test_config)
    assert note_parent == book_id


def test_run_sync_reads_plugin_settings_saved_after_startup(test_config, monkeypatch) -> None:
    """A `lestash config set` made while the server runs reaches the next sync (#208)."""
    from lestash.core.config import Config

    seen: list[dict] = []

    class _Recorder:
        def sync(self, config: dict):
            seen.append(config)
            return iter(())

    monkeypatch.setattr(deps, "_config", test_config)  # the copy cached at startup
    monkeypatch.setattr(sources_route, "load_plugins", lambda: {"audible": _Recorder()})
    saved = Config(general=test_config.general, audible={"locale": "uk"})
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: saved))

    sources_route._run_sync("audible")
    assert seen == [{"locale": "uk"}]
