"""Gemini Apps activity from Takeout (lestash.importers.gemini_activity) and its merging."""

import io
import json
import zipfile

import pytest
from lestash.core.database import get_connection
from lestash.core.sync import SyncStore
from lestash.importers import merge, parse_zip
from lestash.importers.gemini_activity import html_to_text, parse_gemini_activity
from lestash.models.item import ItemCreate

CHAT = "9a092ad0271efb86"
OTHER = "35f191aa6c979db4"
PATH = "Takeout/My Activity/Gemini Apps/My Activity.json"


def _prompt(text: str, when: str, *chats: str, reply: str = "<p>Answer.</p>", **extra) -> dict:
    entry = {
        "header": "Gemini Apps",
        "title": f"Prompted {text}",
        "time": when,
        "products": ["Gemini Apps"],
        "details": [
            {
                "name": f"https://gemini.google.com/app/{c}",
                "url": f"https://gemini.google.com/app/{c}",
            }
            for c in chats or (CHAT,)
        ],
        "activityControls": ["Gemini Apps Activity"],
    }
    if reply:
        entry["safeHtmlItem"] = [{"html": reply}]
    entry.update(extra)
    return entry


def _zip(entries: list[dict]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(PATH, json.dumps(entries))
    return buf.getvalue()


def _import(test_db, entries: list[dict]) -> SyncStore:
    items, _ = parse_zip(_zip(entries))
    with get_connection(test_db) as conn:
        store = SyncStore(conn, "takeout")
        store.run(iter(items))
        return store


def _items(test_db, where: str = "1", params: tuple = ()) -> list[dict]:
    with get_connection(test_db) as conn:
        rows = conn.execute(
            "SELECT id, source_id, title, content, author, parent_id, created_at, metadata "
            f"FROM items WHERE source_type = 'gemini' AND {where} ORDER BY id",
            params,
        ).fetchall()
    keys = ("id", "source_id", "title", "content", "author", "parent_id", "created_at")
    return [
        {**dict(zip(keys, row[:7], strict=True)), "metadata": json.loads(row[7] or "{}")}
        for row in rows
    ]


def _page_chat(test_db, turns: list[tuple[str, str]], chat: str = CHAT) -> int:
    """A chat saved from the Gemini page, as the browser extension stores it."""
    with get_connection(test_db) as conn:
        (chat_id,) = conn.execute(
            "INSERT INTO items (source_type, source_id, title, content, metadata) VALUES "
            "('gemini', 'gemini-search-abc', 'Page title', 'Gemini conversation with 2 "
            "messages.', ?) RETURNING id",
            (json.dumps({"source": "search_page_import", "conversation_id": f"c_{chat}"}),),
        ).fetchone()
        for n, (author, text) in enumerate(turns, 1):
            conn.execute(
                "INSERT INTO items (source_type, source_id, content, author, parent_id, metadata)"
                " VALUES ('gemini', ?, ?, ?, ?, ?)",
                (f"c_{chat}-{author}-{n}", text, author, chat_id, json.dumps({"role": author})),
            )
        conn.commit()
    return chat_id


def _snapshot(test_db) -> list[tuple]:
    with get_connection(test_db) as conn:
        return conn.execute("SELECT * FROM items ORDER BY id").fetchall()


# --- parsing ------------------------------------------------------------------------------


def test_prompts_become_a_chat_with_ordered_turns(test_db) -> None:
    _import(
        test_db,
        [
            _prompt("Second   question", "2026-11-05T15:20:00.000Z", reply="<p>B &amp; C</p>"),
            _prompt("First question", "2026-11-05T15:11:06.941Z", reply="<ul><li>one</li></ul>"),
        ],
    )
    chat, *turns = _items(test_db)
    assert chat["source_id"] == f"c_{CHAT}"
    assert chat["title"] == "First question"
    assert chat["content"] == "Gemini conversation with 2 prompts."
    assert chat["metadata"]["prompted_count"] == 2
    assert chat["metadata"]["conversation_id"] == f"c_{CHAT}"
    assert [(t["author"], t["content"], t["parent_id"]) for t in turns] == [
        ("user", "First question", chat["id"]),
        ("model", "- one", chat["id"]),
        ("user", "Second   question", chat["id"]),
        ("model", "B & C", chat["id"]),
    ]
    assert turns[0]["created_at"] < turns[1]["created_at"] < turns[2]["created_at"]
    # The private matching keys are not stored
    assert not any(k.startswith("_o") or k == "_prompt" for t in turns for k in t["metadata"])


def test_attachments_and_notebook_are_kept_by_name() -> None:
    entry = _prompt(
        "Summarise",
        "2026-11-05T15:11:06.941Z",
        attachedFiles=["paper.pdf"],
        imageFile="shot.png",
        subtitles=[
            {"name": "Attached 2 files."},
            {"name": "Ontologies  was used in this chat. Manage your NotebookLM notebooks"},
            {"name": "1 generated image."},
        ],
    )
    chat, prompt, _reply = parse_gemini_activity([entry])
    assert prompt.metadata["attachments"] == ["paper.pdf", "shot.png"]
    assert prompt.metadata["notebooklm_notebook"] == "Ontologies"
    assert prompt.metadata["generated_images"] == 1
    assert chat.metadata["notebooklm_notebooks"] == ["Ontologies"]


def test_a_prompt_naming_two_chats_is_a_turn_of_each() -> None:
    items = parse_gemini_activity([_prompt("Translate", "2026-05-16T14:12:49.079Z", CHAT, OTHER)])
    assert sorted(i.source_id for i in items if i.metadata.get("type") == "chat") == sorted(
        [f"c_{CHAT}", f"c_{OTHER}"]
    )


def test_a_canvas_is_its_own_item_with_no_chat() -> None:
    canvas = {
        "header": "Gemini Apps",
        "title": "Created Gemini Canvas titled Bridging note",
        "time": "2026-11-05T11:44:18.462Z",
        "subtitles": [{"name": "# Draft 1"}, {"name": "# Draft 2"}, {"name": "# Draft 1"}],
    }
    (item,) = parse_gemini_activity([canvas])
    assert item.title == "Bridging note"
    assert item.content == "# Draft 1\n\n---\n\n# Draft 2"
    assert item.metadata == {"source": "takeout_activity", "type": "canvas", "draft_count": 2}
    assert item.parent_id is None and "_parent_source_id" not in item.metadata


def test_other_activity_is_skipped() -> None:
    entries = [
        {"header": "Gemini Apps", "title": t, "time": "2026-11-04T16:56:03.577Z"}
        for t in ("Cleared previous feedback", "Gave feedback: Good response", "Used Gemini Apps")
    ]
    assert parse_gemini_activity(entries) == []


def test_html_to_text_keeps_blocks_and_unescapes() -> None:
    html = "<h3>Title</h3><p>a &quot;b&quot;</p><ol><li>x</li><li>y</li></ol><script>no</script>"
    assert html_to_text(html) == 'Title\n\na "b"\n\n- x\n- y'
    assert html_to_text("<ul>\n<li>\n<p><strong>Top:</strong> one</p>\n</li></ul>") == "- Top: one"
    assert html_to_text("<ul><li>a<ul><li>. b</li></ul></li></ul>") == "- a\n  - . b"


# --- merging ------------------------------------------------------------------------------


def test_a_reimport_merges_prompt_times_and_changes_nothing_else(test_db) -> None:
    older = _prompt("First question", "2026-01-01T10:00:00.000Z")
    newer = _prompt("Later question", "2026-11-05T15:11:06.941Z")
    _import(test_db, [older, newer])
    before = _snapshot(test_db)
    _import(test_db, [older, newer])
    assert _snapshot(test_db) == before

    # An export after Gemini dropped the older prompt keeps it, its title and its count
    _import(test_db, [newer])
    chat = _items(test_db, "source_id = ?", (f"c_{CHAT}",))[0]
    assert chat["title"] == "First question"
    assert chat["metadata"]["prompted_count"] == 2
    assert chat["content"] == "Gemini conversation with 2 prompts."
    assert len(_items(test_db, "parent_id = ?", (chat["id"],))) == 4


def test_a_chat_saved_from_the_page_is_kept_and_gains_only_new_prompts(test_db) -> None:
    chat_id = _page_chat(test_db, [("user", "First  question\n"), ("model", "Page answer [1]")])
    page_before = _snapshot(test_db)
    store = _import(
        test_db,
        [
            _prompt("First question", "2026-01-01T10:00:00.000Z", reply="<p>Takeout .</p>"),
            _prompt("New question", "2026-01-02T10:00:00.000Z"),
        ],
    )

    # The page's chat and turns are untouched, and no second chat appears
    assert _snapshot(test_db)[: len(page_before)] == page_before
    assert [c["source_id"] for c in _items(test_db, "parent_id IS NULL")] == ["gemini-search-abc"]
    added = _items(test_db, "id > ?", (page_before[-1][0],))
    assert [(t["author"], t["content"], t["parent_id"]) for t in added] == [
        ("user", "New question", chat_id),
        ("model", "Answer.", chat_id),
    ]
    assert all(t["metadata"]["_parent_source_id"] == "gemini-search-abc" for t in added)
    assert store.items == 2

    # Importing again changes nothing
    before = _snapshot(test_db)
    _import(test_db, [_prompt("First question", "2026-01-01T10:00:00.000Z")])
    assert _snapshot(test_db) == before


def test_repeated_prompts_match_page_turns_one_to_one(test_db) -> None:
    _page_chat(test_db, [("user", "continue"), ("model", "page reply")])
    _import(
        test_db,
        [
            _prompt("continue", "2026-01-01T10:00:00.000Z"),
            _prompt("continue", "2026-01-01T11:00:00.000Z", reply="<p>second</p>"),
        ],
    )
    added = _items(test_db, "source_id LIKE ?", ("%-prompt-%",))
    assert [t["created_at"][:13] for t in added] == ["2026-01-01 11"]


def test_reply_links_are_kept_and_full_stop_lines_flagged() -> None:
    cited = (
        '<p>Do raises against a wall [<a href="https://www.youtube.com/watch?v=x&amp;t=45">'
        '00:45</a>] and see <a href="https://arxiv.org/abs/1">https://arxiv.org/abs/1</a>.</p>'
    )
    dropped = "<p>.</p><h3>Ranking</h3><ul><li>. For a Linux drive</li></ul>"
    trailing = "<p><strong>The Nature of Mind</strong> . . .</p>"
    clean = "<p>Use λx . x + 1 here.</p>"
    replies = [
        parse_gemini_activity([_prompt("q", "2026-01-01T10:00:00.000Z", reply=html)])[2]
        for html in (cited, dropped, trailing, clean)
    ]
    assert replies[0].content == (
        "Do raises against a wall [[00:45](https://www.youtube.com/watch?v=x&t=45)] "
        "and see https://arxiv.org/abs/1."
    )
    assert replies[0].metadata["link_count"] == 2
    assert [r.metadata.get("takeout_gaps") for r in replies] == [None, True, None, None]
    assert replies[1].content == ".\n\nRanking\n\n- . For a Linux drive"  # left as it is


def test_two_page_saves_of_one_chat_use_the_one_with_turns(test_db) -> None:
    with get_connection(test_db) as conn:
        conn.execute(
            "INSERT INTO items (source_type, source_id, content, metadata) VALUES "
            "('gemini', 'gemini-search-stub', 'stub', ?)",
            (json.dumps({"source": "search_page_import", "conversation_id": f"c_{CHAT}"}),),
        )
        conn.commit()
    chat_id = _page_chat(test_db, [("user", "Old")])
    _import(test_db, [_prompt("New", "2026-01-02T10:00:00.000Z")])
    assert {t["parent_id"] for t in _items(test_db, "source_id LIKE 'c_%-prompt-%'")} == {chat_id}
    stub = _items(test_db, "source_id = 'gemini-search-stub'")[0]
    assert stub["content"] == "stub"


def test_stubs_without_a_chat_id_are_left_alone(test_db) -> None:
    with get_connection(test_db) as conn:
        conn.execute(
            "INSERT INTO items (source_type, source_id, title, content, metadata) VALUES "
            "('gemini', 'gemini-search-x', 'First question', 'stub', ?)",
            (json.dumps({"source": "search_page_import", "import_status": "stub"}),),
        )
        conn.commit()
    before = _snapshot(test_db)
    _import(test_db, [_prompt("First question", "2026-01-01T10:00:00.000Z")])
    assert _snapshot(test_db)[:1] == before
    assert len(_items(test_db, "parent_id IS NULL")) == 2  # no guess: a separate chat


def test_the_server_import_skips_what_the_page_chat_holds(test_db) -> None:
    pytest.importorskip("lestash_server")
    from lestash_server.routes.imports import _insert_items_with_parents

    chat_id = _page_chat(test_db, [("user", "First question"), ("model", "Page answer")])
    items, _ = parse_zip(
        _zip(
            [
                _prompt("First question", "2026-01-01T10:00:00.000Z"),
                _prompt("New question", "2026-01-02T10:00:00.000Z"),
            ]
        )
    )
    with get_connection(test_db) as conn:
        added, errors = _insert_items_with_parents(conn, items)
    assert (added, errors) == (2, [])
    new = _items(test_db, "source_id LIKE 'c_%-prompt-%' OR source_id LIKE 'c_%-reply-%'")
    assert {t["parent_id"] for t in new} == {chat_id}


def test_merge_leaves_other_items_alone(test_db) -> None:
    item = ItemCreate(source_type="gemini", source_id="x", content="y", metadata={"a": 1})
    with get_connection(test_db) as conn:
        assert merge.merge_with_stored(conn, item) is True
    assert item.metadata == {"a": 1}
