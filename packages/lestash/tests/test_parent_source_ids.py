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


def test_swept_links_are_tagged_sync_in_history(test_db) -> None:
    with get_connection(test_db) as conn:
        upsert_item(conn, _note(1))
        upsert_item(conn, _book())
        assert resolve_parent_source_ids(conn, "audible") == 1
        note_id = conn.execute(
            "SELECT id FROM items WHERE source_id = 'audible:annotation:B1:1'"
        ).fetchone()[0]
        reasons = [
            r[0]
            for r in conn.execute(
                "SELECT change_reason FROM item_history"
                " WHERE item_id = ? AND parent_id_old IS NULL",
                (note_id,),
            )
        ]
        assert reasons == ["sync"]


def test_resolve_parent_source_ids_accepts_several_source_types(test_db) -> None:
    with get_connection(test_db) as conn:
        upsert_item(conn, _note(1))
        upsert_item(conn, _book())
        nb_child = ItemCreate(
            source_type="notebooklm",
            source_id="nb/Notes/a.html",
            content="n",
            metadata={"_parent_source_id": "nb"},
        )
        upsert_item(conn, nb_child)
        nb_id = upsert_item(conn, ItemCreate(source_type="notebooklm", source_id="nb", content="x"))

        assert resolve_parent_source_ids(conn, []) == 0
        assert resolve_parent_source_ids(conn, {"notebooklm"}) == 1
        assert _parent_of(conn, "nb/Notes/a.html") == nb_id
        assert _parent_of(conn, "audible:annotation:B1:1") is None


class _ChildFirstPlugin:
    """A plugin named "takeout" whose items use other source types, child first."""

    name = "takeout"

    def sync(self, config: dict) -> Iterator[ItemCreate]:
        yield ItemCreate(
            source_type="notebooklm",
            source_id="nb/Notes/a.html",
            content="note",
            metadata={"_parent_source_id": "nb"},
        )
        yield ItemCreate(source_type="notebooklm", source_id="nb", content="notebook")


def test_sources_sync_sweeps_the_source_types_it_wrote(test_db, monkeypatch) -> None:
    monkeypatch.setattr(sources_cli, "load_plugins", lambda: {"takeout": _ChildFirstPlugin()})
    monkeypatch.setattr(sources_cli.Config, "load", classmethod(lambda cls: test_db))

    sources_cli.sync_source(source_name="takeout", all_sources=False)

    with get_connection(test_db) as conn:
        nb_id = conn.execute("SELECT id FROM items WHERE source_id = 'nb'").fetchone()[0]
        assert _parent_of(conn, "nb/Notes/a.html") == nb_id


class _NoParentPlugin:
    """Yields a child without naming a parent, like a LinkedIn reaction."""

    name = "audible"

    def sync(self, config: dict) -> Iterator[ItemCreate]:
        yield ItemCreate(source_type="audible", source_id="reaction:1", content="liked")


def test_sources_sync_keeps_a_parent_set_by_a_resolver(test_db, monkeypatch) -> None:
    monkeypatch.setattr(sources_cli, "load_plugins", lambda: {"audible": _NoParentPlugin()})
    monkeypatch.setattr(sources_cli.Config, "load", classmethod(lambda cls: test_db))

    sources_cli.sync_source(source_name="audible", all_sources=False)
    with get_connection(test_db) as conn:
        post_id = upsert_item(
            conn, ItemCreate(source_type="audible", source_id="post", content="p")
        )
        conn.execute("UPDATE items SET parent_id = ? WHERE source_id = 'reaction:1'", (post_id,))
        conn.commit()
        history_before = conn.execute("SELECT COUNT(*) FROM item_history").fetchone()[0]

    sources_cli.sync_source(source_name="audible", all_sources=False)

    with get_connection(test_db) as conn:
        assert _parent_of(conn, "reaction:1") == post_id
        # No unlink/relink churn in the audit trail
        assert conn.execute("SELECT COUNT(*) FROM item_history").fetchone()[0] == history_before


def test_an_item_naming_itself_is_not_its_own_parent(test_db) -> None:
    selfish = ItemCreate(
        source_type="audible",
        source_id="audible:book:B1",
        content="A book",
        metadata={"_parent_source_id": "audible:book:B1"},
    )
    with get_connection(test_db) as conn:
        upsert_item(conn, selfish)
        assert resolve_parent_source_ids(conn, "audible") == 0
        assert _parent_of(conn, "audible:book:B1") is None

        again = selfish.model_copy(deep=True)
        resolve_parent_id(conn, again)  # a re-sync finds the item itself
        assert again.parent_id is None


def test_sweep_skips_a_row_with_malformed_metadata(test_db) -> None:
    with get_connection(test_db) as conn:
        upsert_item(conn, _note(1))
        upsert_item(conn, ItemCreate(source_type="audible", source_id="broken", content="x"))
        conn.execute("UPDATE items SET metadata = '{not json' WHERE source_id = 'broken'")
        book_id = upsert_item(conn, _book())

        assert resolve_parent_source_ids(conn, "audible") == 1
        assert _parent_of(conn, "audible:annotation:B1:1") == book_id
        assert _parent_of(conn, "broken") is None


def test_upsert_item_keeps_a_parent_when_none_is_given(test_db) -> None:
    """Core upsert_item callers (audible books, voice, YouTube routes) pass no parent."""
    with get_connection(test_db) as conn:
        book_id = upsert_item(conn, _book())
        child_id = upsert_item(conn, ItemCreate(source_type="audible", source_id="c", content="c"))
        conn.execute("UPDATE items SET parent_id = ? WHERE id = ?", (book_id, child_id))
        conn.commit()
        history_before = conn.execute("SELECT COUNT(*) FROM item_history").fetchone()[0]

        upsert_item(conn, ItemCreate(source_type="audible", source_id="c", content="c"))
        assert _parent_of(conn, "c") == book_id
        # Nothing changed, so nothing is written to the audit trail
        assert conn.execute("SELECT COUNT(*) FROM item_history").fetchone()[0] == history_before

        # An explicit parent still replaces it
        other_id = upsert_item(conn, ItemCreate(source_type="audible", source_id="o", content="o"))
        moved = ItemCreate(source_type="audible", source_id="c", content="c", parent_id=other_id)
        upsert_item(conn, moved)
        assert _parent_of(conn, "c") == other_id
