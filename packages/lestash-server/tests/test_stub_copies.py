"""A stub's page: named by its post, with copies of it kept under it (#322)."""

from unittest.mock import patch

from lestash.core.database import get_connection

POST = "urn:li:activity:7526276556013355009"


def _stub(test_config, cached: bool = True) -> int:
    with get_connection(test_config) as conn:
        stub = conn.execute(
            "INSERT INTO items (source_type, source_id, content, held, url, metadata)"
            " VALUES ('linkedin', ?, '', 0, ?, ?) RETURNING id",
            (
                f"stub:{POST}",
                f"https://www.linkedin.com/feed/update/{POST}",
                f'{{"post_urn": "{POST}"}}',
            ),
        ).fetchone()[0]
        if cached:
            conn.execute(
                "INSERT INTO post_cache (urn, author_name, content_preview, source)"
                " VALUES (?, 'a-company', 'Black Friday &amp; more', 'feed_preview')",
                (POST,),
            )
        conn.commit()
    return stub


def test_a_stub_is_named_by_its_posts_preview(client, test_config):
    stub = _stub(test_config)
    item = client.get(f"/api/items/{stub}").json()
    assert (item["title"], item["author_display"], item["copies"]) == (
        "Black Friday & more",
        "a-company",
        0,
    )


def test_a_pasted_copy_is_a_child_by_the_posts_author_not_yours(client, test_config):
    stub = _stub(test_config)
    resp = client.post(f"/api/items/{stub}/copies", json={"content": "  The whole post.  "})
    assert resp.status_code == 201
    copy = resp.json()
    assert (copy["parent_id"], copy["content"], copy["is_own_content"], copy["author"]) == (
        stub,
        "The whole post.",
        False,
        "a-company",
    )
    assert (copy["kind_label"], copy["metadata"]["copy_of"]) == ("Copy", stub)
    shot = client.post(f"/api/items/{stub}/copies", json={"kind": "image"}).json()
    assert (shot["source_type"], shot["title"], shot["parent_id"]) == ("image", "Screenshot", stub)
    assert client.get(f"/api/items/{stub}").json()["copies"] == 2

    # Nothing to keep, or an item LeStash holds already
    assert client.post(f"/api/items/{stub}/copies", json={"content": " "}).status_code == 422
    held = copy["id"]
    assert client.post(f"/api/items/{held}/copies", json={"content": "x"}).status_code == 409


def test_a_copy_is_found_by_search_as_its_stub(client, test_config):
    stub = _stub(test_config)
    client.post(f"/api/items/{stub}/copies", json={"content": "zanzibarish quarterly figures"})
    found = client.get("/api/items/search", params={"q": "zanzibarish", "mode": "keyword"}).json()[
        "items"
    ]
    assert [(i["id"], i["title"]) for i in found] == [(stub, "Black Friday & more")]


def test_fetching_a_preview_names_the_stub(client, test_config):
    stub = _stub(test_config, cached=False)
    assert client.get(f"/api/items/{stub}").json()["title"] in (None, "")
    page = {
        "status": "200",
        "title": "t",
        "author": "Ann Lee",
        "handle": "ann",
        "og_url": "https://www.linkedin.com/posts/ann_x",
        "description": "Fresh text",
        "error": None,
    }
    with patch("lestash_linkedin.feed_preview.fetch_preview", return_value=page) as fetch:
        item = client.post(f"/api/items/{stub}/preview").json()
    fetch.assert_called_once_with("7526276556013355009", urn_kind="activity")
    assert (item["title"], item["author_display"]) == ("Fresh text", "Ann Lee")
    with patch(
        "lestash_linkedin.feed_preview.fetch_preview",
        return_value={**page, "status": "429", "og_url": None},
    ):
        assert client.post(f"/api/items/{stub}/preview").status_code == 429
