"""POST /api/import links children to parents already in the DB, and keeps those links."""

import io
import json
import zipfile

from lestash.core.database import get_connection, upsert_item
from lestash.models.item import ItemCreate

VID = "AAAAAAAAAAA"


def _history_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "Takeout/YouTube and YouTube Music/history/watch-history.json",
            json.dumps(
                [
                    {
                        "header": "YouTube",
                        "title": "Watched Markov blankets explained",
                        "titleUrl": f"https://www.youtube.com/watch?v={VID}",
                        "time": "2026-01-01T10:00:00Z",
                    }
                ]
            ),
        )
    return buf.getvalue()


def _post(client) -> None:
    resp = client.post(
        "/api/import",
        files={"file": ("takeout.zip", io.BytesIO(_history_zip()), "application/zip")},
    )
    assert resp.status_code == 200, resp.text


def _parent(test_config) -> int | None:
    with get_connection(test_config) as conn:
        return conn.execute(
            "SELECT parent_id FROM items WHERE source_id = ?", (f"history:{VID}",)
        ).fetchone()[0]


def test_watched_video_nests_under_its_liked_video(client, test_config) -> None:
    with get_connection(test_config) as conn:
        liked_id = upsert_item(
            conn, ItemCreate(source_type="youtube", source_id=f"liked:{VID}", content="liked")
        )
    _post(client)
    assert _parent(test_config) == liked_id
    _post(client)  # re-import keeps the link
    assert _parent(test_config) == liked_id


def test_unresolved_parent_marker_is_kept_for_a_later_sync(client, test_config) -> None:
    _post(client)
    assert _parent(test_config) is None
    with get_connection(test_config) as conn:
        md = json.loads(
            conn.execute(
                "SELECT metadata FROM items WHERE source_id = ?", (f"history:{VID}",)
            ).fetchone()[0]
        )
    assert md["_parent_source_id"] == f"liked:{VID}"


def _nb(name: str) -> ItemCreate:
    return ItemCreate(source_type="notebooklm", source_id=f"nb:{name}", content=name)


def _note(nb: str, n: int) -> ItemCreate:
    return ItemCreate(
        source_type="notebooklm",
        source_id=f"nb:{nb}/note{n}",
        content=f"note {n}",
        metadata={"_parent_source_id": f"nb:{nb}"},
    )


def test_reimport_batch_nests_children_under_their_own_parent(test_config) -> None:
    """A re-imported parent after a new one in the same batch: its upsert hits ON CONFLICT,
    where lastrowid still holds the new parent's id."""
    from lestash_server.routes.imports import _insert_items_with_parents

    with get_connection(test_config) as conn:
        _insert_items_with_parents(conn, [_nb("A"), _note("A", 1)])
    with get_connection(test_config) as conn:
        _insert_items_with_parents(conn, [_nb("B"), _note("B", 1), _nb("A"), _note("A", 2)])
        ids = dict(conn.execute("SELECT source_id, id FROM items WHERE source_type = 'notebooklm'"))
        parents = dict(
            conn.execute("SELECT source_id, parent_id FROM items WHERE source_type = 'notebooklm'")
        )
    assert parents["nb:A/note1"] == ids["nb:A"]
    assert parents["nb:A/note2"] == ids["nb:A"]
    assert parents["nb:B/note1"] == ids["nb:B"]


def test_drive_imports_run_off_the_event_loop() -> None:
    """They download and parse synchronously (Takeout parts are several GB)."""
    import inspect

    from lestash_server.routes import imports

    assert not inspect.iscoroutinefunction(imports.import_from_drive)
    assert not inspect.iscoroutinefunction(imports.sync_from_drive)
