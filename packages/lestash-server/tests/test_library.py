"""The Library (#264): shelves, their facets, and the year and sort filters."""

from lestash.core.database import get_connection


def test_shelves_count_what_they_hold(client):
    shelves = {s["shelf"]: s for s in client.get("/api/library").json()}
    assert list(shelves) == ["books", "papers", "videos", "notebooks", "posts", "chats", "notes"]
    assert (shelves["papers"]["count"], shelves["videos"]["count"]) == (1, 1)
    assert shelves["notebooks"]["kinds"] == ["notebook", "project"]


def test_a_shelf_has_facets_by_person_topic_and_year(client, test_config):
    with get_connection(test_config) as conn:
        video = conn.execute("SELECT id FROM items WHERE source_id = 'yt-1'").fetchone()[0]
        person = conn.execute(
            "INSERT INTO people (name, kind) VALUES ('TechChannel', 'organization')"
        ).lastrowid
        conn.execute(
            "INSERT INTO item_agents (item_id, person_id, role, origin)"
            " VALUES (?, ?, 'creator', 'test')",
            (video, person),
        )
        tag = conn.execute("INSERT INTO tags (name) VALUES ('learning')").lastrowid
        conn.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (video, tag))
        conn.commit()
    facets = client.get("/api/library/videos/facets").json()
    assert facets["people"] == [{"id": person, "name": "TechChannel", "count": 1}]
    assert facets["topics"] == [{"name": "learning", "count": 1}]
    assert facets["years"] == [{"name": "2024", "count": 1}]
    assert client.get("/api/library/nope/facets").status_code == 404


def test_items_filter_by_year_and_sort_by_title(client):
    assert client.get("/api/items?year=2024").json()["total"] == 5
    assert client.get("/api/items?year=2023").json()["total"] == 0
    assert client.get("/api/items?year=24").status_code == 422
    listed = client.get("/api/items?sort=title&kind=paper,video").json()["items"]
    titles = [i["title"] for i in listed]
    assert titles == sorted(titles, key=str.lower)
    oldest = client.get("/api/items?sort=oldest").json()["items"]
    assert oldest[0]["source_id"] == "li-1"


def test_a_shelf_list_matches_its_facet_counts(client, test_config):
    """With a topic or person filter, a shelf still lists top-level items only (#264)."""
    with get_connection(test_config) as conn:
        video = conn.execute("SELECT id FROM items WHERE source_id = 'yt-1'").fetchone()[0]
        child = conn.execute(
            "INSERT INTO items (source_type, source_id, content, parent_id, metadata)"
            " VALUES ('youtube', 'yt-child', 'part', ?, '{\"source_subtype\": \"history\"}')",
            (video,),
        ).lastrowid
        tag = conn.execute("INSERT INTO tags (name) VALUES ('t2')").lastrowid
        person = conn.execute("INSERT INTO people (name, kind) VALUES ('P', 'person')").lastrowid
        for item in (video, child):
            conn.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (item, tag))
            conn.execute(
                "INSERT INTO item_agents (item_id, person_id, role, origin)"
                " VALUES (?, ?, 'publisher', 'test')",
                (item, person),
            )
        conn.commit()
    facets = client.get("/api/library/videos/facets").json()
    assert {t["name"]: t["count"] for t in facets["topics"]}["t2"] == 1
    assert {p["name"]: p["count"] for p in facets["people"]}["P"] == 1
    for f in ("tag=t2", f"person={person}"):
        listed = client.get(f"/api/items?kind=video&top_level=true&{f}").json()
        assert [i["id"] for i in listed["items"]] == [video]
