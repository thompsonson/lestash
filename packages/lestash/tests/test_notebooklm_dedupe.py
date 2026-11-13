"""NotebookLM notebooks held twice under two folder names (cut short, or renamed)."""

import io
import json
import zipfile

from lestash.core.database import get_connection
from lestash.core.sync import SyncStore
from lestash.importers import parse_zip
from lestash.importers.notebooklm_dedupe import dedupe_notebooklm, find_duplicates

UUID = "f7591865-c06a-489a-b047-52b5ac69f9ea"
CREATED = "2026-03-01T10:00:00.123456Z"
FULL = "Takeout/NotebookLM/Adaptive Intelligence_ Frontiers in Continual Learning"
CUT = FULL[:50]  # older exports cut folder and file names short


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def _export(folder: str, *, notebook_id: str | None, chat: str, title: str = "") -> dict:
    meta: dict = {
        "title": title or "Adaptive Intelligence: Frontiers in Continual Learning",
        "metadata": {"createTime": CREATED},
    }
    if notebook_id:
        meta["id"] = notebook_id
    return {
        f"{folder}/{folder.rsplit('/', 1)[-1]} metadata.json": json.dumps(meta),
        f"{folder}/Notes/Plan A.html": "<p>first plan</p>",
        f"{folder}/Chat History/{chat}.html": "<p>USER: hi MODEL: hello</p>",
    }


def _sync(conn, files: dict) -> None:
    """What a Takeout sync does: store every item, then sweep (dedupe, links)."""
    items, _ = parse_zip(_zip(files))
    SyncStore(conn, "takeout").run(items)


def _notebooks(conn) -> list[tuple[int, str]]:
    return conn.execute(
        "SELECT id, source_id FROM items WHERE source_type = 'notebooklm' "
        "AND parent_id IS NULL ORDER BY id"
    ).fetchall()


def _children(conn, parent_id: int) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT source_id FROM items WHERE parent_id = ? ORDER BY source_id", (parent_id,)
        )
    ]


def test_the_notebook_id_is_kept(test_db) -> None:
    with get_connection(test_db) as conn:
        _sync(conn, _export(FULL, notebook_id=UUID, chat="Chat Session - abc"))
        (raw,) = conn.execute("SELECT metadata FROM items WHERE source_id = ?", (FULL,)).fetchone()
    assert json.loads(raw)["notebook_id"] == UUID


def test_a_cut_folder_name_is_merged_into_the_full_one_with_its_children(test_db) -> None:
    with get_connection(test_db) as conn:
        # The old export, stored before notebook ids were kept
        _sync(conn, _export(CUT, notebook_id=None, chat="Chat Session - cac7dc5e-5d2a-4cd5"))
        ((old_id, _),) = _notebooks(conn)
        note = conn.execute(
            "INSERT INTO items (source_type, content, parent_id) VALUES ('note', 'mine', ?) "
            "RETURNING id",
            (old_id,),
        ).fetchone()[0]
        tag = conn.execute("INSERT INTO tags (name) VALUES ('t') RETURNING id").fetchone()[0]
        conn.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (old_id, tag))
        conn.commit()

        _sync(
            conn,
            _export(FULL, notebook_id=UUID, chat="Chat Session - cac7dc5e-5d2a-4cd5-8cde-2097"),
        )

        ((current_id, source_id),) = _notebooks(conn)
        children = _children(conn, current_id)
        tags = conn.execute("SELECT item_id FROM item_tags WHERE tag_id = ?", (tag,)).fetchall()
    assert source_id == FULL and current_id != old_id
    # One note and one chat: the chat's cut name matched the full one
    assert [c for c in children if c] == [
        f"{FULL}/Chat History/Chat Session - cac7dc5e-5d2a-4cd5-8cde-2097.html",
        f"{FULL}/Notes/Plan A.html",
    ]
    assert len(children) == 3  # and your own note, moved with them
    assert conn_parent(test_db, note) == current_id
    assert [tuple(row) for row in tags] == [(current_id,)]


def conn_parent(test_db, item_id: int) -> int:
    with get_connection(test_db) as conn:
        return conn.execute("SELECT parent_id FROM items WHERE id = ?", (item_id,)).fetchone()[0]


def test_a_renamed_notebook_is_merged_by_its_id(test_db) -> None:
    renamed = "Takeout/NotebookLM/Continual Learning"
    with get_connection(test_db) as conn:
        _sync(conn, _export(FULL, notebook_id=UUID, chat="Chat 1"))
        _sync(
            conn,
            {
                **_export(renamed, notebook_id=UUID, chat="Chat 1", title="Continual Learning"),
            },
        )
        ((current_id, source_id),) = _notebooks(conn)
        (title,) = conn.execute("SELECT title FROM items WHERE id = ?", (current_id,)).fetchone()
        children = _children(conn, current_id)
    assert source_id == renamed and title == "Continual Learning"
    assert children == [f"{renamed}/Chat History/Chat 1.html", f"{renamed}/Notes/Plan A.html"]


def test_different_notebooks_are_left_alone(test_db) -> None:
    other = "Takeout/NotebookLM/Other"
    with get_connection(test_db) as conn:
        _sync(conn, _export(FULL, notebook_id=UUID, chat="Chat 1"))
        _sync(
            conn,
            {
                f"{other}/Other metadata.json": json.dumps(
                    # Same title, another time: two notebooks
                    {
                        "title": "Adaptive Intelligence: Frontiers in Continual Learning",
                        "metadata": {"createTime": "2026-04-01T10:00:00Z"},
                        "id": "another",
                    }
                ),
                f"{other}/Notes/Plan A.html": "<p>x</p>",
            },
        )
        assert len(_notebooks(conn)) == 2
        assert find_duplicates(conn) == []


def test_running_again_changes_nothing(test_db) -> None:
    with get_connection(test_db) as conn:
        _sync(conn, _export(CUT, notebook_id=None, chat="Chat 1"))
        _sync(conn, _export(FULL, notebook_id=UUID, chat="Chat 1"))
        before = conn.execute("SELECT COUNT(*) FROM items").fetchone()
        assert dedupe_notebooklm(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone() == before


def test_a_cut_name_matching_two_children_is_left_alone() -> None:
    from lestash.importers.notebooklm_dedupe import _child_pairs

    old = [(1, "chat", "Chat Session - ab")]
    current = [(2, "chat", "Chat Session - abc"), (3, "chat", "Chat Session - abd")]
    assert _child_pairs(old, current) == []
    assert _child_pairs(old, current[:1]) == [(1, 2)]
    # Kinds must match
    assert _child_pairs([(1, "note", "Chat Session - abc")], current) == []


def test_a_cut_artifact_name_loses_what_is_left_of_metadata() -> None:
    from lestash.importers.notebooklm_dedupe import _child_name

    cut = "Takeout/NotebookLM/X/Artifacts/Training Private AI With Guardrail Traces meta"
    full = "Takeout/NotebookLM/X/Artifacts/Training Private AI With Guardrail Traces metadata"
    assert _child_name(cut, "artifact") == "Training Private AI With Guardrail Traces"
    assert _child_name(full, "artifact") == "Training Private AI With Guardrail Traces"
    # Other kinds keep their names
    assert _child_name("Takeout/NotebookLM/X/Notes/Plan me.html", "note") == "Plan me"
