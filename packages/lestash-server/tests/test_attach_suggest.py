"""Items to attach to before you search (#324)."""

from lestash.core.database import get_connection
from lestash_server.routes.items import link_match

POST = "7526276556013355009"


def test_a_link_is_known_by_its_id_or_its_whole_address():
    shared = f"https://www.linkedin.com/posts/a-company_deal-activity-{POST}-AbCd?utm_source=x"
    assert link_match(shared) == (f"%{POST}%", None)
    assert link_match(f"https://www.linkedin.com/feed/update/urn:li:activity:{POST}/") == (
        f"%{POST}%",
        None,
    )
    assert link_match("https://youtu.be/dQw4w9WgXcQ?si=x") == ("%dQw4w9WgXcQ%", None)
    assert link_match("https://www.youtube.com/watch?app=1&v=dQw4w9WgXcQ") == (
        "%dQw4w9WgXcQ%",
        None,
    )
    assert link_match("HTTPS://www.Example.org/a_b/?utm_source=x#top") == (
        "%example.org/a\\_b%",
        "example.org/a_b",
    )
    assert link_match("https://example.org/item?b=2&a=1&si=x") == (
        "%example.org/item%",
        "example.org/item?a=1&b=2",
    )
    assert link_match("  ") is None


def _item(conn, source_type, source_id, content="x", **cols):
    cols = {"source_type": source_type, "source_id": source_id, "content": content, **cols}
    marks = ", ".join("?" * len(cols))
    return conn.execute(
        f"INSERT INTO items ({', '.join(cols)}) VALUES ({marks}) RETURNING id", list(cols.values())
    ).fetchone()[0]


def test_suggestions_put_the_shared_links_item_first_then_your_work(client, test_config):
    with get_connection(test_config) as conn:
        stub = _item(
            conn,
            "linkedin",
            f"stub:urn:li:activity:{POST}",
            held=0,
            content="",
            url=f"https://www.linkedin.com/feed/update/urn:li:activity:{POST}",
            metadata=f'{{"post_urn": "urn:li:activity:{POST}"}}',
        )
        # Your reaction names the post too, but is never a place to attach to
        _item(
            conn,
            "linkedin",
            "changelog-socialActions/likes-1",
            content=f"😂 ENTERTAINMENT on activity:{POST}",
            metadata='{"resource_name": "socialActions/likes"}',
        )
        conn.execute(
            "INSERT INTO post_cache (urn, content_preview, source) VALUES (?, ?, 'feed_preview')",
            (f"urn:li:activity:{POST}", "Black Friday &amp; more"),
        )
        page = _item(conn, "web", "page", url="https://example.org/a", title="A page")
        _item(conn, "web", "front", url="https://example.org/", title="Front page")
        noted = _item(conn, "web", "noted", url="https://other.org/n", title="Noted")
        _item(conn, "note", "note-1", parent_id=noted, created_at="2099-01-01 00:00:00")
        conn.commit()

    got = client.get("/api/items/suggest", params={"url": f"https://lnkd.in/x-activity-{POST}"})
    body = got.json()
    assert [(i["id"], i["title"]) for i in body["same_link"]] == [(stub, "Black Friday & more")]
    assert [i["id"] for i in body["worked_on"]][:1] == [noted]

    body = client.get(
        "/api/items/suggest", params={"url": "https://www.example.org/a/?utm_source=x"}
    ).json()
    assert [i["id"] for i in body["same_link"]] == [page]

    # The item open is never offered
    body = client.get("/api/items/suggest", params={"exclude": noted}).json()
    assert noted not in [i["id"] for i in body["worked_on"]]
    assert body["same_link"] == []


def test_the_exact_address_is_found_among_many_that_contain_it(client, test_config):
    with get_connection(test_config) as conn:
        exact = _item(conn, "web", "exact", url="https://blog.example/post", title="The post")
        for n in range(60):  # newer pages under it, each containing its address
            _item(conn, "web", f"sub-{n}", url=f"https://blog.example/post/{n}", title=f"{n}")
        conn.commit()
    body = client.get("/api/items/suggest", params={"url": "http://blog.example/post?ref=x"})
    assert [i["id"] for i in body.json()["same_link"]] == [exact]


def test_parameters_that_name_a_page_count_and_tracking_ones_do_not(client, test_config):
    with get_connection(test_config) as conn:
        one = _item(conn, "web", "hn-1", url="https://news.ycombinator.com/item?id=1", title="1")
        _item(conn, "web", "hn-2", url="https://news.ycombinator.com/item?id=2", title="2")
        conn.commit()
    shared = "https://news.ycombinator.com/item?utm_source=x&id=1"
    body = client.get("/api/items/suggest", params={"url": shared}).json()
    assert [i["id"] for i in body["same_link"]] == [one]
