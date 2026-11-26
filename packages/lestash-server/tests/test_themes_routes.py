"""Suggested themes in Collections and themes (#266): listed, kept, dismissed."""

import itertools
import json

import lestash_server.deps as deps
import pytest
from lestash.core.database import get_connection
from lestash.core.themes import find_themes

TITLES = ["Intelligent agents", "Agents in practice", "Agent theory", "BDI agents"]
SOURCES = ["kobo", "arxiv", "google-drive", "kobo"]


@pytest.fixture
def theme(test_config):
    with get_connection(test_config) as conn:
        ids = [
            conn.execute(
                "INSERT INTO items (source_type, source_id, title, content) VALUES (?, ?, ?, ?)",
                (source, f"t{n}", title, "text"),
            ).lastrowid
            for n, (title, source) in enumerate(zip(TITLES, SOURCES, strict=True))
        ]
        for a, b in itertools.combinations(ids, 2):
            conn.execute(
                "INSERT INTO item_links (from_id, to_id, kind, evidence, origin, status) "
                "VALUES (?, ?, 'related', ?, 'linker:vectors', 'suggested')",
                (a, b, json.dumps({"score": 0.8, "found_by": [a]})),
            )
        find_themes(conn)
        conn.commit()
    return ids


def test_themes_list_their_items_and_sources(client, theme):
    data = client.get("/api/themes").json()

    assert len(data) == 1
    assert [i["id"] for i in data[0]["items"]] == theme
    assert data[0]["sources"] == ["kobo", "arxiv", "google-drive"]
    assert "agent" in data[0]["name"].lower()


def test_keeping_a_theme_makes_a_collection(client, theme):
    theme_id = client.get("/api/themes").json()[0]["id"]

    resp = client.post(f"/api/themes/{theme_id}/keep", json={"name": "Agents"})

    assert resp.status_code == 201, resp.text
    collection = client.get(f"/api/collections/{resp.json()['collection_id']}").json()
    assert collection["name"] == "Agents"
    assert client.get("/api/themes").json() == []
    assert client.post(f"/api/themes/{theme_id}/keep").status_code == 404


def test_dismissing_a_theme_hides_it(client, theme):
    theme_id = client.get("/api/themes").json()[0]["id"]

    assert client.post(f"/api/themes/{theme_id}/dismiss").json() == {"status": "dismissed"}

    assert client.get("/api/themes").json() == []
    assert client.post(f"/api/themes/{theme_id}/dismiss").status_code == 404
    with deps.get_db() as conn:
        status = conn.execute("SELECT status FROM themes WHERE id = ?", (theme_id,)).fetchone()[0]
    assert status == "dismissed"


def test_an_untitled_item_is_listed_by_its_text_and_kind(client, theme):
    long_text = "First words of a post\n  that runs on " + "and on " * 20
    with deps.get_db() as conn:
        conn.execute(
            "UPDATE items SET title = NULL, content = ? WHERE id = ?", (long_text, theme[0])
        )
        conn.commit()

    items = client.get("/api/themes").json()[0]["items"]

    untitled = next(i for i in items if i["id"] == theme[0])
    assert untitled["excerpt"].startswith("First words of a post that runs on")
    assert untitled["excerpt"].endswith("…") and len(untitled["excerpt"]) <= 90
    titled = next(i for i in items if i["id"] == theme[1])
    assert titled["excerpt"] is None
    assert titled["kind_label"]
