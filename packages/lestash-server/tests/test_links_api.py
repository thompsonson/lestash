"""GET and DELETE /api/items/{id}/links: an item's links to and from other items."""

from lestash.core.database import get_connection
from lestash.core.links import link_items


def _pair(test_config) -> tuple[int, int]:
    with get_connection(test_config) as conn:
        post = conn.execute(
            "INSERT INTO items (source_type, source_id, url, title, content) "
            "VALUES ('microblog', 'p', 'https://matt.example/p', 'A post', '') RETURNING id"
        ).fetchone()[0]
        chat = conn.execute(
            "INSERT INTO items (source_type, source_id, content) "
            "VALUES ('claude', 'c', 'Read https://matt.example/p') RETURNING id"
        ).fetchone()[0]
        link_items(conn)
        conn.commit()
    return chat, post


def test_links_are_listed_both_ways_and_a_removed_one_goes(client, test_config) -> None:
    chat, post = _pair(test_config)
    [link] = client.get(f"/api/items/{chat}/links").json()["links"]
    assert (link["direction"], link["kind"], link["item"]["title"]) == (
        "outgoing",
        "references",
        "A post",
    )
    [back] = client.get(f"/api/items/{post}/links").json()["links"]
    assert (back["direction"], back["item"]["id"]) == ("incoming", chat)

    query = {"other_id": chat, "kind": "references", "direction": "incoming"}
    assert client.delete(f"/api/items/{post}/links", params=query).json() == {"status": "rejected"}
    assert client.get(f"/api/items/{chat}/links").json() == {"links": []}
    assert client.delete(f"/api/items/{post}/links", params=query).status_code == 200


def test_a_missing_item_or_link_is_404(client, test_config) -> None:
    chat, post = _pair(test_config)
    assert client.get("/api/items/999999/links").status_code == 404
    query = {"other_id": post, "kind": "syndication"}
    assert client.delete(f"/api/items/{chat}/links", params=query).status_code == 404
