"""Stubs stay out of listings, search and stats; their detail view says what they are."""

from lestash.core.database import get_connection
from lestash.core.stubs import make_stub


def _stub_and_item(test_config) -> tuple[int, int]:
    with get_connection(test_config) as conn:
        item = conn.execute(
            "INSERT INTO items (source_type, source_id, content) "
            "VALUES ('linkedin', 'c', 'A comment on a post') RETURNING id"
        ).fetchone()[0]
        stub = make_stub(conn, "linkedin", "urn:li:activity:1", url="https://x.example/1")
        conn.execute("UPDATE items SET title = 'A comment' WHERE id = ?", (stub,))
        conn.commit()
    return stub, item


def test_a_stub_is_left_out_of_listings_search_and_stats(client, test_config) -> None:
    before = client.get("/api/stats").json()
    stub, item = _stub_and_item(test_config)
    listed = client.get("/api/items", params={"limit": 100}).json()
    assert item in [i["id"] for i in listed["items"]]
    assert stub not in [i["id"] for i in listed["items"]]
    assert listed["total"] == before["total_items"] + 1
    by_source = client.get("/api/items", params={"source": "linkedin"}).json()["items"]
    assert stub not in [i["id"] for i in by_source]
    found = client.get("/api/items/search", params={"q": "comment", "mode": "keyword"}).json()
    assert [i["id"] for i in found["items"]] == [item]
    stats = client.get("/api/stats").json()
    assert stats["total_items"] == before["total_items"] + 1
    assert stats["sources"]["linkedin"] == before["sources"].get("linkedin", 0) + 1


def test_a_stubs_detail_says_it_is_not_held(client, test_config) -> None:
    stub, item = _stub_and_item(test_config)
    assert client.get(f"/api/items/{stub}").json()["held"] is False
    assert client.get(f"/api/items/{item}").json()["held"] is True


def test_a_stub_cannot_be_edited(client, test_config) -> None:
    stub, item = _stub_and_item(test_config)
    assert client.patch(f"/api/items/{stub}", json={"content": "x"}).status_code == 409
    assert client.patch(f"/api/items/{item}", json={"content": "x"}).status_code == 200
