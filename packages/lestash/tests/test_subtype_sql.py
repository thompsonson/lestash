"""subtype_sql must give the same string as get_item_subtype, so SQL filters match the UI."""

import json
import sqlite3

import pytest
from lestash.core.enrichment import get_item_subtype, subtype_excludes_sql, subtype_sql
from lestash.models.item import Item


def _item(metadata):
    return Item(id=1, source_type="linkedin", content="", fetched_at=NOW, metadata=metadata)


NOW = "2026-01-01T00:00:00"
CASES = [
    None,
    {},
    {"resource_name": ""},
    {"resource_name": "ugcPosts"},
    {"resource_name": "socialActions/comments"},
    {"resource_name": "socialActions/likes"},
    {"resource_name": "socialActions/likes", "reaction_type": "CELEBRATE"},
    {"resource_name": "socialActions/likes", "reaction_type": ""},
    {"resource_name": "socialActions/likes", "reacted_to": "urn:li:activity:1"},
    {"resource_name": "socialActions/likes", "reacted_to": "urn:li:comment:(x,1)"},
    {"resource_name": "socialActions/likes", "reaction_type": "LIKE", "reacted_to": ""},
    {"resource_name": "invitations"},
    {"resource_name": "messages"},
    {"resource_name": "people/positions"},
    {"resource_name": "socialActions/shares"},
    {"type": "notebook"},
    {"resource_name": 0},
    {"resource_name": False},
    {"resource_name": "socialActions/likes", "reaction_type": 0},
    {"resource_name": "socialActions/likes", "reacted_to": 0},
]


@pytest.mark.parametrize("metadata", CASES)
def test_sql_matches_python(metadata):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE items (source_type TEXT, metadata TEXT)")
    stored = json.dumps(metadata) if metadata is not None else None
    conn.execute("INSERT INTO items VALUES ('linkedin', ?)", (stored,))
    (from_sql,) = conn.execute(f"SELECT {subtype_sql()} FROM items").fetchone()
    item = _item(metadata)
    assert from_sql == get_item_subtype(item)


def test_invalid_json_is_plain_source():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE items (source_type TEXT, metadata TEXT)")
    conn.execute("INSERT INTO items VALUES ('bluesky', '{not json')")
    assert conn.execute(f"SELECT {subtype_sql()} FROM items").fetchone()[0] == "bluesky"


@pytest.mark.parametrize("metadata", CASES)
@pytest.mark.parametrize("terms", [["reaction"], ["post", "message"], ["linked"], ["comment"]])
def test_excludes_match_python(metadata, terms):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE items (source_type TEXT, metadata TEXT)")
    stored = json.dumps(metadata) if metadata is not None else None
    conn.execute("INSERT INTO items VALUES ('linkedin', ?)", (stored,))
    clause, params = subtype_excludes_sql(terms)
    kept = conn.execute(f"SELECT count(*) FROM items WHERE {clause}", params).fetchone()[0]
    item = _item(metadata)
    subtype = get_item_subtype(item)
    assert kept == (0 if any(t in subtype for t in terms) else 1)
