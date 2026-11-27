"""The Claude data export ZIP (#88): conversations, projects and memories."""

import io
import json
import zipfile

from lestash.core.database import get_connection
from lestash.importers import parse_zip

CONV = "c0000000-0000-4000-8000-000000000001"
PROJECT = "p0000000-0000-4000-8000-000000000001"


def _message(uuid, sender, text, **extra):
    return {
        "uuid": uuid,
        "text": text,
        "content": [{"type": "text", "text": text}] if text else [],
        "sender": sender,
        "created_at": "2026-03-01T10:00:00.000000Z",
        "updated_at": "2026-03-01T10:00:00.000000Z",
        "attachments": [],
        "files": [],
        **extra,
    }


def _export(conversations=None, projects=None, memories=None, users=True) -> bytes:
    files = {"conversations.json": conversations if conversations is not None else []}
    if users:
        files["users.json"] = [{"uuid": "u1", "full_name": "Test User"}]
    if projects is not None:
        files["projects.json"] = projects
    if memories is not None:
        files["memories.json"] = memories
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, json.dumps(data))
    return buf.getvalue()


LONG = "Explain hierarchical task networks and how they differ from classical planning, briefly."
CONVERSATION = {
    "uuid": CONV,
    "name": "HTN planning",
    "summary": "",
    "created_at": "2026-03-01T09:59:00.000000Z",
    "updated_at": "2026-03-01T10:05:00.000000Z",
    "account": {"uuid": "u1"},
    "chat_messages": [
        _message("m1", "human", LONG),
        # A turn of tool calls only has no text: it is left out
        _message("m2", "assistant", "", content=[{"type": "tool_use", "name": "search"}]),
        _message(
            "m3",
            "assistant",
            "HTN planning decomposes tasks.",
            attachments=[
                {
                    "file_name": "notes.txt",
                    "file_size": 12,
                    "file_type": "text/plain",
                    "extracted_content": "secret",
                }
            ],
            files=[{"file_name": "diagram.png"}],
        ),
    ],
}


def _by_id(items):
    return {i.source_id: i for i in items}


def test_a_conversation_is_a_parent_with_its_messages_as_children():
    items, kind = parse_zip(_export([CONVERSATION]))
    assert kind == "claude"
    found = _by_id(items)
    assert list(found) == [CONV, "m1", "m3"]

    conv = found[CONV]
    assert (conv.source_type, conv.title, conv.is_own_content) == ("claude", "HTN planning", True)
    # No summary: the start of the first message stands in
    assert conv.content == LONG[:200]
    assert conv.metadata == {
        "source": "export",
        "message_count": 3,
        "has_attachments": True,
        "has_files": True,
    }

    mine, reply = found["m1"], found["m3"]
    assert mine.title == LONG[:80] + "..."
    assert (mine.is_own_content, mine.author) == (True, None)
    assert mine.metadata == {"sender": "human", "_parent_source_id": CONV}
    assert reply.title is None and reply.is_own_content is False
    assert reply.metadata == {
        "sender": "assistant",
        "_parent_source_id": CONV,
        "attachments": [{"file_name": "notes.txt", "file_size": 12, "file_type": "text/plain"}],
        "files": ["diagram.png"],
    }
    assert reply.created_at.isoformat() == "2026-03-01T10:00:00+00:00"


def test_a_summary_is_the_conversation_content():
    items, _ = parse_zip(_export([{**CONVERSATION, "summary": "**Overview** of HTN."}]))
    assert _by_id(items)[CONV].content == "**Overview** of HTN."


def test_a_conversation_with_nothing_to_read():
    empty = {**CONVERSATION, "name": "", "chat_messages": [_message("m9", "human", "")]}
    items, _ = parse_zip(_export([empty]))
    assert [(i.title, i.content) for i in items] == [("", "(empty conversation)")]


def test_a_message_with_its_text_only_in_content_blocks():
    msg = _message("m1", "human", "")
    msg["content"] = [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "Hello"}]
    items, _ = parse_zip(_export([{**CONVERSATION, "chat_messages": [msg]}]))
    assert _by_id(items)["m1"].content == "Hello"


def test_projects_with_their_documents():
    projects = [
        {
            "uuid": PROJECT,
            "name": "Thesis",
            "description": "Writing up.",
            "prompt_template": "Be concise.",
            "is_private": True,
            "created_at": "2025-04-06T13:44:44.403632+00:00",
            "docs": [
                {
                    "uuid": "d1",
                    "filename": "outline.md",
                    "content": "# Outline",
                    "created_at": "2025-04-07T10:00:00+00:00",
                },
                {"uuid": "d2", "filename": "empty.md", "content": "", "created_at": None},
            ],
        },
        {"uuid": "p2", "name": "Bare", "description": "", "prompt_template": "", "docs": []},
    ]
    items, _ = parse_zip(_export(projects=projects))
    found = _by_id(items)
    assert list(found) == [PROJECT, "d1", "p2"]  # an empty document is left out
    project = found[PROJECT]
    assert project.source_type == "claude-project"
    assert project.content == "Writing up.\n\n---\n\n**Prompt template:**\nBe concise."
    assert project.metadata == {"source": "export", "doc_count": 2, "is_private": True}
    assert found["d1"].metadata == {
        "source": "export",
        "type": "doc",
        "_parent_source_id": PROJECT,
    }
    assert (found["d1"].title, found["d1"].content) == ("outline.md", "# Outline")
    assert found["p2"].content == "(no description)"


def test_memories():
    memories = [
        {
            "conversations_memory": "Prefers short answers.",
            "project_memories": {PROJECT: "Thesis is on HTN.", "p2": ""},
            "account_uuid": "u1",
        }
    ]
    items, _ = parse_zip(_export(memories=memories))
    found = _by_id(items)
    assert list(found) == ["conversations-memory", f"project-memory-{PROJECT}"]
    assert found["conversations-memory"].title == "Conversation Memory"
    project = found[f"project-memory-{PROJECT}"]
    assert project.source_type == "claude-memory"
    assert project.title == "Project Memory (p0000000...)"
    assert project.metadata == {"source": "export", "type": "project", "project_uuid": PROJECT}
    assert project.created_at is not None


def test_a_chatgpt_export_is_not_taken_for_a_claude_one():
    """ChatGPT's export also has conversations.json, with user.json beside it."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("conversations.json", json.dumps([{"title": "x", "mapping": {}}]))
        zf.writestr("user.json", json.dumps({"id": "u"}))
    try:
        _items, kind = parse_zip(buf.getvalue())
    except ValueError:
        kind = None
    assert kind != "claude"


def test_importing_an_export_again_adds_no_copies(test_db):
    """Server and CLI insert through _insert_items_with_parents; this uses the core upsert."""
    from lestash.core.database import resolve_parent_id, upsert_item

    data = _export([CONVERSATION])
    for _ in range(2):
        items, _ = parse_zip(data)
        with get_connection(test_db) as conn:
            for item in items:
                resolve_parent_id(conn, item)
                upsert_item(conn, item)
    with get_connection(test_db) as conn:
        rows = conn.execute(
            "SELECT i.source_id, p.source_id FROM items i LEFT JOIN items p ON p.id = i.parent_id"
            " WHERE i.source_type = 'claude' ORDER BY i.id"
        ).fetchall()
    assert [tuple(r) for r in rows] == [(CONV, None), ("m1", CONV), ("m3", CONV)]
