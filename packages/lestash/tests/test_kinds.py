"""items.kind (SQL, migration 24) and item_kind (Python) give the same kind."""

import json
import sqlite3

import pytest
from lestash.core.kinds import KIND_SQL, KINDS, item_kind, kind_detail

CASES = [
    ("linkedin", {"resource_name": "ugcPosts"}, None, True, "post"),
    ("linkedin", {"resource_name": "instantReposts"}, None, True, "post"),
    ("linkedin", {"resource_name": "socialActions/likes"}, 1, True, "reaction"),
    ("linkedin", {"resource_name": "socialActions/comments"}, None, True, "comment"),
    ("linkedin", {"resource_name": "messages"}, None, True, "message"),
    ("linkedin", {"resource_name": "invitations"}, None, True, "message"),
    ("linkedin", {"resource_name": "people/positions"}, None, True, "other"),
    ("linkedin", {"resource_name": 5}, None, True, "other"),
    ("linkedin", None, None, False, "stub"),
    ("bluesky", None, None, True, "post"),
    ("bluesky", None, 3, True, "comment"),
    ("microblog", {}, None, True, "post"),
    ("youtube", {"source_subtype": "history"}, None, True, "video"),
    ("youtube", {"source_subtype": "liked"}, None, True, "video"),
    ("youtube", {"source_subtype": "post"}, None, True, "post"),
    ("youtube", {"source_subtype": "search"}, None, True, "search"),
    ("youtube", {"type": "transcript"}, 4, True, "transcript"),
    ("arxiv", None, None, True, "paper"),
    ("kobo", {"kind": "store"}, None, True, "book"),
    ("kobo", {"kind": "drive_pdf"}, None, True, "book"),
    ("kobo", {"kind": "highlight"}, 2, True, "highlight"),
    ("kobo", {"kind": "dogear"}, 2, True, "highlight"),
    ("audible", {"type": "book"}, None, True, "book"),
    ("audible", {"type": "audible.clip"}, 2, True, "highlight"),
    ("jelu", None, None, True, "book"),
    ("claude", None, None, True, "chat"),
    ("claude", None, 9, True, "turn"),
    ("claude-code", None, None, True, "chat"),
    ("mistral", None, 9, True, "turn"),
    ("gemini", {"type": "chat"}, None, True, "chat"),
    ("gemini", None, 9, True, "turn"),
    ("gemini", {"type": "canvas"}, None, True, "document"),
    ("notebooklm", {"type": "notebook"}, None, True, "notebook"),
    ("notebooklm", {"type": "artifact", "artifact_type": "slides"}, 1, True, "artifact"),
    ("notebooklm", {"type": "report"}, 1, True, "artifact"),
    ("notebooklm", {"type": "chat"}, 1, True, "chat"),
    ("notebooklm", {"type": "note"}, 1, True, "note"),
    ("notebooklm", {"type": "source"}, 1, True, "document"),
    ("claude-project", None, None, True, "project"),
    ("claude-project", {"type": "doc"}, 1, True, "document"),
    ("gmail", {"type": "thread"}, None, True, "email"),
    ("email", None, None, True, "email"),
    ("email", None, 2, True, "message"),
    ("voice", None, None, True, "note"),
    ("claude-memory", {"type": "project"}, None, True, "note"),
    ("image", None, 1, True, "image"),
    ("google-drive", None, None, True, "document"),
    ("share", None, None, True, "document"),
    ("something-new", None, None, True, "document"),
]


def _sql_kind(source_type, metadata, parent_id, held, raw=None):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE items (source_type TEXT, metadata TEXT, parent_id INTEGER, held INTEGER,"
        f" kind TEXT GENERATED ALWAYS AS ({KIND_SQL}) VIRTUAL)"
    )
    stored = raw if raw is not None else (json.dumps(metadata) if metadata is not None else None)
    conn.execute("INSERT INTO items VALUES (?, ?, ?, ?)", (source_type, stored, parent_id, held))
    return conn.execute("SELECT kind FROM items").fetchone()[0]


@pytest.mark.parametrize(("source", "metadata", "parent", "held", "expected"), CASES)
def test_sql_and_python_agree(source, metadata, parent, held, expected):
    assert item_kind(source, metadata, parent, held) == expected
    assert _sql_kind(source, metadata, parent, int(held)) == expected


def test_invalid_metadata_is_read_as_none():
    assert _sql_kind("youtube", None, None, 1, raw="{not json") == "video"
    assert _sql_kind("notebooklm", None, None, 1, raw="{not json") == "document"


def test_every_kind_has_a_label():
    assert {c[-1] for c in CASES} <= set(KINDS)


def test_artifact_detail_in_words():
    assert kind_detail("artifact", {"artifact_type": "audio_overview"}) == "Audio overview"
    assert kind_detail("artifact", {"type": "report"}) == "Report"
    assert kind_detail("video", {}) is None
