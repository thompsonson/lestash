"""The sync loop shared by the CLI and the server (lestash.core.sync)."""

from __future__ import annotations

from collections.abc import Iterator

import lestash.core.sync as core_sync
import pytest
import typer
from lestash.cli import sources as sources_cli
from lestash.core.database import get_connection
from lestash.models.item import ItemCreate


def _item(source_id: str, parent: str | None = None) -> ItemCreate:
    return ItemCreate(
        source_type="audible",
        source_id=source_id,
        content=source_id,
        metadata={"_parent_source_id": parent} if parent else None,
    )


def _sync(test_db, monkeypatch, plugin) -> int:
    """The CLI's exit code: 1 when the sync failed (#209)."""
    monkeypatch.setattr(sources_cli, "load_plugins", lambda: {"audible": plugin})
    monkeypatch.setattr(sources_cli.Config, "load", classmethod(lambda cls: test_db))
    try:
        sources_cli.sync_source(source_name="audible", all_sources=False)
    except typer.Exit as e:
        return e.exit_code
    return 0


def _last_sync(test_db) -> tuple[str, int]:
    with get_connection(test_db) as conn:
        return tuple(
            conn.execute(
                "SELECT status, items_added FROM sync_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
        )


def _last_counts(test_db) -> tuple[int, int]:
    with get_connection(test_db) as conn:
        return tuple(
            conn.execute(
                "SELECT items_added, items_updated FROM sync_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
        )


class _Yields:
    def __init__(self, *items: ItemCreate) -> None:
        self.items = items

    def sync(self, config: dict) -> Iterator[ItemCreate]:
        yield from self.items


def test_a_sync_counts_new_changed_and_unchanged_items_apart(test_db, monkeypatch) -> None:
    """#210: a re-import doesn't count every item as added again."""
    assert _sync(test_db, monkeypatch, _Yields(_item("a"), _item("b"))) == 0
    assert _last_counts(test_db) == (2, 0)

    # The same items again: nothing new, nothing changed
    assert _sync(test_db, monkeypatch, _Yields(_item("a"), _item("b"))) == 0
    assert _last_counts(test_db) == (0, 0)

    # One changed, one new, one the same
    changed = _item("a")
    changed.content = "edited"
    assert _sync(test_db, monkeypatch, _Yields(changed, _item("b"), _item("c"))) == 0
    assert _last_counts(test_db) == (1, 1)


def test_new_media_on_a_stored_item_counts_as_an_update(test_db, monkeypatch) -> None:
    from lestash.models.item import MediaCreate

    assert _sync(test_db, monkeypatch, _Yields(_item("a"))) == 0
    with_cover = _item("a")
    with_cover.media = [MediaCreate(media_type="image", url="https://example.org/a.jpg")]
    assert _sync(test_db, monkeypatch, _Yields(with_cover)) == 0
    assert _last_counts(test_db) == (0, 1)
    assert _sync(test_db, monkeypatch, _Yields(with_cover)) == 0  # the same media again
    assert _last_counts(test_db) == (0, 0)


def _with_metadata(source_id: str, metadata: dict) -> ItemCreate:
    return ItemCreate(source_type="audible", source_id=source_id, content="x", metadata=metadata)


def _history_rows(test_db) -> int:
    with get_connection(test_db) as conn:
        return conn.execute("SELECT COUNT(*) FROM item_history").fetchone()[0]


def test_an_unchanged_item_is_not_written_again(test_db, monkeypatch) -> None:
    item = _with_metadata("a", {"times": [1, 2], "kind": "book"})
    assert _sync(test_db, monkeypatch, _Yields(item)) == 0
    history = _history_rows(test_db)
    assert _sync(test_db, monkeypatch, _Yields(item)) == 0
    assert _last_counts(test_db) == (0, 0)
    assert _history_rows(test_db) == history  # no history row for a no-op re-import


def test_a_changed_item_is_counted_as_its_history_records_it(test_db, monkeypatch) -> None:
    """The count and item_history agree: both compare the stored text."""
    assert _sync(test_db, monkeypatch, _Yields(_with_metadata("a", {"x": 1, "y": 2}))) == 0
    history = _history_rows(test_db)
    assert _sync(test_db, monkeypatch, _Yields(_with_metadata("a", {"y": 2, "x": 1}))) == 0
    assert _last_counts(test_db) == (0, 1)
    assert _history_rows(test_db) == history + 1


def test_an_item_yielded_twice_in_a_run_is_new_once(test_db, monkeypatch) -> None:
    assert _sync(test_db, monkeypatch, _Yields(_item("a"), _item("a"))) == 0
    assert _last_counts(test_db) == (1, 0)  # the second write changed nothing


def test_items_without_a_source_id_each_count_as_new(test_db) -> None:
    items = [ItemCreate(source_type="note", content=f"n{i}") for i in range(3)]
    with get_connection(test_db) as conn:
        store = core_sync.SyncStore(conn, "note")
        store.run(iter(items))
    assert store.counts == core_sync.SyncCounts(added=3)


def test_an_item_whose_media_fails_is_still_counted(test_db) -> None:
    from lestash.models.item import MediaCreate

    with_cover = _item("a")
    with_cover.media = [MediaCreate(media_type="image", url="https://example.org/a.jpg")]

    def broken_media(*args, **kwargs):
        raise RuntimeError("disk full")

    with get_connection(test_db) as conn:
        store = core_sync.SyncStore(conn, "audible")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(core_sync, "add_item_media", broken_media)
            with pytest.raises(RuntimeError):
                store.run(iter([_item("b"), with_cover]))
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
    assert store.counts == core_sync.SyncCounts(added=2)
    assert store.committed == store.counts  # the failed run commits what it wrote


def test_a_parent_kept_from_a_resolver_is_not_a_change(test_db, monkeypatch) -> None:
    """The upsert keeps a stored parent when the plugin names none."""
    assert _sync(test_db, monkeypatch, _Yields(_item("book"), _item("note"))) == 0
    with get_connection(test_db) as conn:  # as a post-sync resolver does
        conn.execute(
            "UPDATE items SET parent_id = (SELECT id FROM items WHERE source_id = 'book')"
            " WHERE source_id = 'note'"
        )
        conn.commit()
    assert _sync(test_db, monkeypatch, _Yields(_item("book"), _item("note"))) == 0
    assert _last_counts(test_db) == (0, 0)


class _ChildThenParentThenFail:
    def sync(self, config: dict) -> Iterator[ItemCreate]:
        yield _item("note", parent="book")
        yield _item("book")
        raise RuntimeError("API went away")


def test_a_failed_sync_still_links_what_it_stored(test_db, monkeypatch) -> None:
    assert _sync(test_db, monkeypatch, _ChildThenParentThenFail()) == 1

    assert _last_sync(test_db) == ("failed", 2)
    with get_connection(test_db) as conn:
        book, note_parent = conn.execute(
            "SELECT b.id, n.parent_id FROM items b, items n"
            " WHERE b.source_id = 'book' AND n.source_id = 'note'"
        ).fetchone()
    assert note_parent == book


class _Watcher:
    """Counts, from another connection, the items committed before each yield."""

    def __init__(self, test_db, n: int) -> None:
        self.test_db, self.n = test_db, n
        self.seen: list[int] = []

    def sync(self, config: dict) -> Iterator[ItemCreate]:
        for i in range(self.n):
            with get_connection(self.test_db) as other:
                self.seen.append(other.execute("SELECT COUNT(*) FROM items").fetchone()[0])
            yield _item(f"i{i}")


def test_a_long_sync_commits_as_it_goes(test_db, monkeypatch) -> None:
    """Other writers (the server, another sync) get the write lock between chunks."""
    monkeypatch.setattr(core_sync, "COMMIT_EVERY", 2, raising=False)
    watcher = _Watcher(test_db, 5)
    _sync(test_db, monkeypatch, watcher)
    assert watcher.seen == [0, 0, 2, 2, 4]
    assert _last_sync(test_db) == ("completed", 5)


def test_connections_wait_for_the_write_lock(test_db) -> None:
    """SQLite's default 5 s isn't long enough to wait out a sync's write."""
    with get_connection(test_db) as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 30_000


class _Interrupted:
    def sync(self, config: dict) -> Iterator[ItemCreate]:
        yield _item("a")
        raise KeyboardInterrupt


def test_an_interrupt_commits_nothing_more(test_db, monkeypatch) -> None:
    with pytest.raises(KeyboardInterrupt):
        _sync(test_db, monkeypatch, _Interrupted())
    with get_connection(test_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
