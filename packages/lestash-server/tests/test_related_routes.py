"""Related items on the item page (#219): suggestions, decisions, and similar items
without the pairs you rejected."""

import json

import lestash_server.deps as deps
import pytest
from lestash.core.database import get_connection


@pytest.fixture
def three(test_config):
    with get_connection(test_config) as conn:
        ids = [
            conn.execute(
                "INSERT INTO items (source_type, source_id, title, content) VALUES (?, ?, ?, ?)",
                (source, f"{source}-1", f"From {source}", "Some text"),
            ).lastrowid
            for source in ("kobo", "arxiv", "youtube")
        ]
        a, b, _ = ids
        conn.execute(
            "INSERT INTO item_links (from_id, to_id, kind, evidence, origin, status) "
            "VALUES (?, ?, 'related', ?, 'linker:vectors', 'suggested')",
            (a, b, json.dumps({"score": 0.71, "model": "m", "found_by": [a]})),
        )
        conn.commit()
    return ids


def test_suggestions_list_the_other_item_with_its_score(client, three):
    a, b, _ = three
    for item, other in ((a, b), (b, a)):
        data = client.get(f"/api/items/{item}/related").json()
        assert [(s["item"]["id"], s["score"]) for s in data["suggestions"]] == [(other, 0.71)]


def test_confirming_makes_a_link_and_ends_the_suggestion(client, three):
    a, b, _ = three
    resp = client.put(f"/api/items/{b}/related/{a}", json={"status": "confirmed"})
    assert resp.status_code == 200, resp.text

    assert client.get(f"/api/items/{a}/related").json()["suggestions"] == []
    links = client.get(f"/api/items/{a}/links").json()["links"]
    assert [(link["kind"], link["item"]["id"]) for link in links] == [("related", b)]


def test_confirmed_items_are_listed_from_both_ends_until_rejected(client, three):
    """#424: a confirmed item shows under Related, not only under Links."""
    a, b, c = three
    assert client.get(f"/api/items/{a}/related").json()["confirmed"] == []
    client.put(f"/api/items/{a}/related/{b}", json={"status": "confirmed"})
    client.put(f"/api/items/{c}/related/{a}", json={"status": "confirmed"})
    for item, others in ((a, {b, c}), (b, {a}), (c, {a})):
        data = client.get(f"/api/items/{item}/related").json()
        assert {other["id"] for other in data["confirmed"]} == others
        assert data["suggestions"] == []
    first = client.get(f"/api/items/{a}/related").json()["confirmed"][0]
    assert first["title"] and first["source_type"]  # a whole item, for its card

    client.put(f"/api/items/{a}/related/{b}", json={"status": "rejected"})
    assert [o["id"] for o in client.get(f"/api/items/{a}/related").json()["confirmed"]] == [c]


def test_a_pair_never_suggested_can_be_decided(client, three):
    a, _, c = three
    assert client.put(f"/api/items/{a}/related/{c}", json={"status": "rejected"}).is_success
    with deps.get_db() as conn:
        row = conn.execute(
            "SELECT origin, status FROM item_links WHERE from_id = ? AND to_id = ?", (a, c)
        ).fetchone()
    assert tuple(row) == ("manual", "rejected")


def test_bad_decisions_are_refused(client, three):
    a, b, _ = three
    assert (
        client.put(f"/api/items/{a}/related/{a}", json={"status": "confirmed"}).status_code == 400
    )
    assert (
        client.put(f"/api/items/{a}/related/99999", json={"status": "confirmed"}).status_code == 404
    )
    assert client.put(f"/api/items/{a}/related/{b}", json={"status": "maybe"}).status_code == 422
    assert client.get("/api/items/99999/related").status_code == 404


def test_similar_items_leave_out_rejected_pairs(client, three, monkeypatch):
    from lestash_server.routes import embeddings as route

    a, b, c = three
    client.put(f"/api/items/{a}/related/{b}", json={"status": "rejected"})
    monkeypatch.setattr(route, "load_vec_extension", lambda conn: None)
    monkeypatch.setattr(route, "ensure_vec_table", lambda conn: None)
    monkeypatch.setattr(route, "stored_vector", lambda conn, item_id: [0.1])
    monkeypatch.setattr(route, "search_similar", lambda conn, e, limit: [(b, 0.1), (c, 0.2)])

    assert [i["id"] for i in client.get(f"/api/embeddings/similar/{a}").json()] == [c]
