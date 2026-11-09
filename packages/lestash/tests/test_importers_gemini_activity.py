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
    assert turns[0]["created_at"] == turns[1]["created_at"] < turns[2]["created_at"]
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


def test_a_prompt_naming_no_chat_is_skipped_and_logged(caplog) -> None:
    entry = _prompt("Lost", "2026-01-01T10:00:00.000Z")
    entry["details"] = []
    assert parse_gemini_activity([entry]) == []
    assert "Skipped 1 Gemini prompts" in caplog.text


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


def test_replies_become_markdown() -> None:
    html = "<h3>Title</h3><p>a &quot;b&quot;</p><ol><li>x</li><li>y</li></ol>"
    assert html_to_text(html) == '### Title\n\na "b"\n\n1. x\n2. y'
    assert html_to_text("<ul>\n<li>\n<p><strong>Top:</strong> one</p>\n</li></ul>") == (
        "- **Top:** one"
    )
    assert html_to_text("<p>Use <code>__init__</code></p>") == "Use `__init__`"
    table = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>y|z</td></tr></table>"
    assert html_to_text(table) == "| A | B |\n| --- | --- |\n| 1 | y\\|z |"
    code = "<pre><code>def a():\n\n\n    pass\n</code></pre>"
    assert html_to_text(code) == "```\ndef a():\n\n\n    pass\n```"


def test_an_ordered_list_with_a_bad_start_still_converts() -> None:
    assert html_to_text("<h2>H</h2><ol start='²'><li>A</li></ol>") == "## H\n\n1. A"


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
    clean = "<p>Use λx . x + 1 here.</p><pre><code>. venv/bin/activate\n</code></pre>"
    replies = [
        parse_gemini_activity([_prompt("q", "2026-01-01T10:00:00.000Z", reply=html)])[2]
        for html in (cited, dropped, trailing, clean)
    ]
    assert replies[0].content == (
        "Do raises against a wall [[00:45](https://www.youtube.com/watch?v=x&t=45)] "
        "and see <https://arxiv.org/abs/1>."
    )
    assert replies[0].metadata["link_count"] == 2
    assert [r.metadata.get("takeout_gaps") for r in replies] == [None, True, None, None]
    assert replies[1].content == ".\n\n### Ranking\n\n- . For a Linux drive"  # left as it is


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


def _stored_chat(test_db, source_id: str, metadata: dict, url: str | None = None) -> int:
    with get_connection(test_db) as conn:
        (chat_id,) = conn.execute(
            "INSERT INTO items (source_type, source_id, url, title, content, metadata) "
            "VALUES ('gemini', ?, ?, 'Saved', 'transcript', ?) RETURNING id",
            (source_id, url, json.dumps(metadata)),
        ).fetchone()
        conn.commit()
    return chat_id


def test_a_chat_saved_by_the_extension_is_kept(test_db) -> None:
    """The extension stores c_<id> with its turns in metadata, not as child items."""
    turns = [{"role": "user", "text": "First question"}, {"role": "model", "text": "Hi [1]"}]
    metadata = {"source": "extension", "conversation_id": f"c_{CHAT}", "turns": turns}
    chat_id = _stored_chat(test_db, f"c_{CHAT}", metadata)
    before = _snapshot(test_db)
    _import(
        test_db,
        [
            _prompt("First question", "2026-01-01T10:00:00.000Z"),
            _prompt("New question", "2026-01-02T10:00:00.000Z"),
        ],
    )
    assert _snapshot(test_db)[:1] == before
    added = _items(test_db, "parent_id = ?", (chat_id,))
    assert [(t["author"], t["content"]) for t in added] == [
        ("user", "New question"),
        ("model", "Answer."),
    ]


def test_a_reply_the_page_lacks_is_added(test_db) -> None:
    """A page saved while the reply was still coming has the prompt only."""
    chat_id = _page_chat(test_db, [("user", "First question")])
    _import(test_db, [_prompt("First question", "2026-01-01T10:00:00.000Z")])
    added = _items(test_db, "parent_id = ? AND source_id LIKE 'c_%-re%'", (chat_id,))
    assert [(t["author"], t["content"]) for t in added] == [("model", "Answer.")]
    # It points at the page's prompt, as Takeout's copy was not stored
    assert added[0]["metadata"]["prompt_source_id"] == f"c_{CHAT}-user-1"


def test_page_prompts_match_without_spaces(test_db) -> None:
    """The HTML page parser joins inline elements: "Explain <b>this</b>" -> "Explainthis"."""
    _page_chat(test_db, [("user", "Explainthis code"), ("model", "Page answer")])
    _import(test_db, [_prompt("Explain this code", "2026-01-01T10:00:00.000Z")])
    assert _items(test_db, "source_id LIKE 'c_%-prompt-%' OR source_id LIKE 'c_%-reply-%'") == []


def test_a_second_accounts_chat_keeps_its_link() -> None:
    entry = _prompt("Hi", "2026-01-01T10:00:00.000Z")
    entry["details"] = [{"url": f"https://gemini.google.com/u/1/app/{CHAT}"}]
    assert (
        parse_gemini_activity([entry, entry])[0].url == f"https://gemini.google.com/u/1/app/{CHAT}"
    )
    chat = parse_gemini_activity([entry, entry])[0]
    assert chat.metadata["prompted_count"] == 1  # an entry listed twice is one prompt


def test_an_older_export_dates_and_names_the_chat_from_its_first_prompt(test_db) -> None:
    _import(test_db, [_prompt("June question", "2026-06-01T10:00:00.000Z")])
    _import(test_db, [_prompt("March question", "2026-03-01T10:00:00.000Z")])
    chat = _items(test_db, "source_id = ?", (f"c_{CHAT}",))[0]
    assert chat["title"] == "March question"
    assert chat["created_at"].startswith("2026-03-01 10:00:00")
    assert chat["metadata"]["prompted_count"] == 2


def test_a_second_account_link_and_odd_entries(caplog) -> None:
    linked = _prompt("Hi", "2026-01-01T10:00:00.000Z")
    linked["details"] = [{"url": f"https://gemini.google.com/u/1/app/{CHAT}?hl=en"}]
    odd = _prompt("Odd", "2026-01-01T11:00:00.000Z")
    odd["safeHtmlItem"] = ["<p>not a dict</p>"]
    items = parse_gemini_activity([linked, odd, {"header": "Gemini Apps", "title": 3}])
    assert [i.source_id for i in items][:1] == [f"c_{CHAT}"]
    assert len(items) == 3  # the chat, the prompt, its reply
    assert "Skipped 1 Gemini activity entries of an unexpected shape" in caplog.text


def test_a_note_on_an_extension_chat_is_not_a_page_turn(test_db) -> None:
    turns = [{"role": "user", "text": "First question"}, {"role": "model", "text": "Hi"}]
    metadata = {"source": "extension", "conversation_id": f"c_{CHAT}", "turns": turns}
    chat_id = _stored_chat(test_db, f"c_{CHAT}", metadata)
    with get_connection(test_db) as conn:
        conn.execute(
            "INSERT INTO items (source_type, source_id, content, author, parent_id) "
            "VALUES ('note', 'n1', 'my note', 'user', ?)",
            (chat_id,),
        )
        conn.commit()
    _import(test_db, [_prompt("First question", "2026-01-01T10:00:00.000Z")])
    assert _items(test_db, "parent_id = ?", (chat_id,)) == []


def test_a_canvas_export_without_its_document_keeps_the_stored_one(test_db) -> None:
    canvas = {
        "header": "Gemini Apps",
        "title": "Created Gemini Canvas titled Plan",
        "time": "2026-11-05T11:44:18.462Z",
        "subtitles": [{"name": "# The plan"}],
    }
    _import(test_db, [canvas])
    _import(test_db, [{**canvas, "subtitles": []}])
    (stored,) = _items(test_db, "source_id LIKE 'canvas-%'")
    assert stored["content"] == "# The plan"


def test_a_renamed_chat_keeps_its_title_on_reimport(test_db) -> None:
    entry = _prompt("First question", "2026-01-01T10:00:00.000Z")
    _import(test_db, [entry])
    with get_connection(test_db) as conn:
        conn.execute("UPDATE items SET title = 'My name' WHERE source_id = ?", (f"c_{CHAT}",))
        conn.commit()
    _import(test_db, [entry, _prompt("Later", "2026-02-01T10:00:00.000Z")])
    assert _items(test_db, "source_id = ?", (f"c_{CHAT}",))[0]["title"] == "My name"


def test_a_renamed_chat_keeps_its_title_when_an_older_export_arrives(test_db) -> None:
    _import(test_db, [_prompt("Second", "2026-02-01T10:00:00.000Z")])
    with get_connection(test_db) as conn:
        conn.execute("UPDATE items SET title = 'My name' WHERE source_id = ?", (f"c_{CHAT}",))
        conn.commit()
    _import(test_db, [_prompt("First", "2026-01-01T10:00:00.000Z")])
    chat = _items(test_db, "source_id = ?", (f"c_{CHAT}",))[0]
    assert chat["title"] == "My name"
    assert chat["created_at"].startswith("2026-01-01")


def test_a_chat_filed_under_another_item_is_still_merged(test_db) -> None:
    _import(test_db, [_prompt("Second", "2026-02-01T10:00:00.000Z")])
    with get_connection(test_db) as conn:
        (folder,) = conn.execute(
            "INSERT INTO items (source_type, source_id, content) VALUES ('note', 'f', 'f') "
            "RETURNING id"
        ).fetchone()
        conn.execute(
            "UPDATE items SET title = 'Mine', parent_id = ? WHERE source_id = ?",
            (folder, f"c_{CHAT}"),
        )
        conn.commit()
    _import(test_db, [_prompt("First", "2026-01-01T10:00:00.000Z")])
    chat = _items(test_db, "source_id = ?", (f"c_{CHAT}",))[0]
    assert (chat["title"], chat["metadata"]["prompted_count"]) == ("Mine", 2)


def test_one_time_written_two_ways_is_one_turn(test_db) -> None:
    _import(test_db, [_prompt("Q", "2026-01-01T10:00:00Z")])
    _import(test_db, [_prompt("Q", "2026-01-01T10:00:00.000123+00:00")])
    _import(test_db, [_prompt("Q", "2026-01-01T10:00:00")])  # no offset: UTC
    assert len(_items(test_db, "parent_id IS NOT NULL")) == 2
    chat = _items(test_db, "source_id = ?", (f"c_{CHAT}",))[0]
    assert "prompted_times" not in chat["metadata"]
    assert chat["metadata"]["prompted_count"] == 1
    with get_connection(test_db) as conn:
        times = conn.execute("SELECT occurred_at FROM activities WHERE verb = 'prompted'")
        assert [r[0] for r in times] == ["2026-01-01T10:00:00.000Z"]


def test_a_long_chats_prompt_count_stays_exact_across_exports(test_db) -> None:
    def prompts(first: int, last: int) -> list[dict]:
        return [
            _prompt(f"p{n}", f"2026-01-01T{n // 60 % 24:02}:{n % 60:02}:00.000Z", reply="")
            for n in range(first, last + 1)
        ]

    _import(test_db, prompts(1, 250))
    _import(test_db, prompts(100, 300))
    chat = _items(test_db, "source_id = ?", (f"c_{CHAT}",))[0]
    assert chat["metadata"]["prompted_count"] == 300
    assert chat["content"] == "Gemini conversation with 300 prompts."


def test_a_prompt_titled_only_prompted_is_kept() -> None:
    entry = _prompt("", "2026-01-01T10:00:00.000Z", attachedFiles=["a.png"])
    entry["title"] = "Prompted"
    chat, prompt, _reply = parse_gemini_activity([entry])
    assert (prompt.content, prompt.metadata["attachments"]) == ("", ["a.png"])


def test_an_attachment_only_prompt_is_not_added_to_a_page_chat(test_db) -> None:
    chat_id = _page_chat(test_db, [("user", ""), ("model", "Answer.")])
    _import(test_db, [_prompt("", "2026-01-01T10:00:00.000Z", attachedFiles=["a.png"])])
    assert len(_items(test_db, "parent_id = ?", (chat_id,))) == 2


def test_pipes_in_nested_table_cells_are_escaped_once() -> None:
    html = "<table><tr><td>a|b<table><tr><td>x|y</td></tr></table></td></tr></table>"
    text = html_to_text(html)
    assert "x\\|y" in text and "x\\\\|y" not in text


def test_a_chat_starting_with_an_attachment_has_a_title() -> None:
    entry = _prompt("", "2026-01-01T10:00:00.000Z", attachedFiles=["a.png"])
    assert parse_gemini_activity([entry])[0].title == "Gemini conversation"


def test_a_chat_saved_as_html_is_matched_by_its_url(test_db) -> None:
    url = f"https://gemini.google.com/u/1/app/{CHAT}?hl=en"
    chat_id = _stored_chat(test_db, url, {"source": "html_import"}, url)
    before = _snapshot(test_db)
    _import(test_db, [_prompt("New question", "2026-01-02T10:00:00.000Z")])
    assert _snapshot(test_db)[:1] == before
    assert len(_items(test_db, "parent_id IS NULL")) == 1
    assert len(_items(test_db, "parent_id = ?", (chat_id,))) == 2
