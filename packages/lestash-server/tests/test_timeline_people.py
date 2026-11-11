"""The Timeline (#262) and People (#263) endpoints."""

from lestash.core.database import get_connection


def _person(conn, name, kind, is_self=0):
    return conn.execute(
        "INSERT INTO people (name, kind, is_self) VALUES (?, ?, ?)", (name, kind, is_self)
    ).lastrowid


def _seed_day(test_config):
    """One day: two watches, one like, a post you made; and one watch the day before."""
    with get_connection(test_config) as conn:
        video = conn.execute("SELECT id FROM items WHERE source_id = 'yt-1'").fetchone()[0]
        for when, verb in (
            ("2026-03-10T09:00:00.000Z", "watched"),
            ("2026-03-10T10:00:00.000Z", "watched"),
            ("2026-03-10T11:00:00.000Z", "liked"),
            ("2026-03-09T08:00:00.000Z", "watched"),
        ):
            conn.execute(
                "INSERT INTO activities (verb, item_id, occurred_at, source)"
                " VALUES (?, ?, ?, 'test')",
                (verb, video, when),
            )
        conn.execute(
            "INSERT INTO items (source_type, source_id, content, created_at, is_own_content,"
            " metadata) VALUES ('linkedin', 'mine', 'my post', '2026-03-10 12:00:00', 1,"
            ' \'{"resource_name": "ugcPosts"}\')'
        )
        conn.commit()
    return video


class TestTimeline:
    def test_a_day_lists_activities_and_what_you_made_newest_first(self, client, test_config):
        video = _seed_day(test_config)
        day = client.get(
            "/api/timeline", params={"start": "2026-03-10T00:00:00Z", "end": "2026-03-11T00:00:00Z"}
        ).json()
        assert [(e["verb"], e["item"]["kind"]) for e in day["entries"]] == [
            ("made", "post"),
            ("liked", "video"),
            ("watched", "video"),
            ("watched", "video"),
        ]
        assert day["entries"][1]["item"]["id"] == video
        assert day["previous"] == "2026-03-09T08:00:00.000Z"
        assert day["next"] is None

    def test_a_zone_offset_moves_the_day(self, client, test_config):
        _seed_day(test_config)
        # 10 Mar 10:30 in UTC-2 starts at 12:30 UTC: nothing is after that
        day = client.get(
            "/api/timeline",
            params={"start": "2026-03-10T10:30:00-02:00", "end": "2026-03-11T00:00:00-02:00"},
        ).json()
        assert [e["verb"] for e in day["entries"]] == []
        day = client.get(
            "/api/timeline",
            params={"start": "2026-03-10T09:30:00-02:00", "end": "2026-03-11T00:00:00-02:00"},
        ).json()
        assert [e["verb"] for e in day["entries"]] == ["made"]

    def test_filters_by_verb_and_kind(self, client, test_config):
        _seed_day(test_config)
        span = {"start": "2026-03-10T00:00:00Z", "end": "2026-03-11T00:00:00Z"}
        watched = client.get("/api/timeline", params={**span, "verb": "watched"}).json()
        assert {e["verb"] for e in watched["entries"]} == {"watched"}
        posts = client.get("/api/timeline", params={**span, "kind": "post"}).json()
        assert [e["verb"] for e in posts["entries"]] == ["made"]

    def test_a_bad_time_is_a_422(self, client):
        resp = client.get("/api/timeline", params={"start": "yesterday", "end": "today"})
        assert resp.status_code == 422


class TestPeople:
    def test_lists_by_switch_with_most_items_first(self, client, test_config):
        with get_connection(test_config) as conn:
            channel = _person(conn, "A Channel", "organization")
            _person(conn, "Quiet Channel", "organization")
            _person(conn, "Claude", "software")
            items = [r[0] for r in conn.execute("SELECT id FROM items LIMIT 2")]
            for item in items:
                conn.execute(
                    "INSERT INTO item_agents (item_id, person_id, role, origin)"
                    " VALUES (?, ?, 'creator', 'test')",
                    (item, channel),
                )
            conn.commit()
        channels = client.get("/api/people", params={"switch": "channels"}).json()
        assert [(p["name"], p["items"]) for p in channels["people"]] == [
            ("A Channel", 2),
            ("Quiet Channel", 0),
        ]
        assert [p["name"] for p in client.get("/api/people?switch=ai").json()["people"]] == [
            "Claude"
        ]
        found = client.get("/api/people", params={"switch": "channels", "q": "quiet"}).json()
        assert [p["name"] for p in found["people"]] == ["Quiet Channel"]
        assert client.get("/api/people?switch=nope").status_code == 422

    def test_a_page_shows_what_they_made_and_your_activity(self, client, test_config):
        video = _seed_day(test_config)
        with get_connection(test_config) as conn:
            channel = _person(conn, "TechChannel", "organization")
            conn.execute(
                "INSERT INTO item_agents (item_id, person_id, role, origin)"
                " VALUES (?, ?, 'creator', 'test')",
                (video, channel),
            )
            conn.commit()
        page = client.get(f"/api/people/{channel}").json()
        assert [(m["role"], m["kind"], m["count"]) for m in page["made"]] == [
            ("creator", "video", 1)
        ]
        assert {a["verb"]: a["count"] for a in page["your_activity"]} == {
            "watched": 3,
            "liked": 1,
        }
        items = client.get("/api/items", params={"person": channel}).json()
        assert [i["id"] for i in items["items"]] == [video]
        assert client.get("/api/people/999999").status_code == 404

    def test_me_is_the_person_marked_as_you(self, client, test_config):
        with get_connection(test_config) as conn:
            _person(conn, "Me Myself", "person", is_self=1)
            conn.commit()
        assert client.get("/api/people/me").json()["is_self"] is True


def test_day_steps_follow_the_filters(client, test_config):
    """A step lands on the nearest day the filters leave something on."""
    _seed_day(test_config)  # 9 Mar has only a watch; 10 Mar has the post you made
    span = {"start": "2026-03-11T00:00:00Z", "end": "2026-03-12T00:00:00Z"}
    made = client.get("/api/timeline", params={**span, "verb": "made"}).json()
    assert made["previous"] == "2026-03-10T12:00:00Z"
    # Unfiltered, the step back from 10 Mar is the watch on 9 Mar; for "made" it
    # skips that day for the seeded posts of 2024
    span = {"start": "2026-03-10T00:00:00Z", "end": "2026-03-11T00:00:00Z"}
    assert client.get("/api/timeline", params=span).json()["previous"].startswith("2026-03-09")
    made = client.get("/api/timeline", params={**span, "verb": "made"}).json()
    assert made["previous"].startswith("2024-")


def test_two_roles_on_one_item_count_once(client, test_config):
    video = _seed_day(test_config)
    with get_connection(test_config) as conn:
        person = _person(conn, "Both", "organization")
        for role in ("creator", "publisher"):
            conn.execute(
                "INSERT INTO item_agents (item_id, person_id, role, origin)"
                " VALUES (?, ?, ?, 'test')",
                (video, person, role),
            )
        conn.commit()
    page = client.get(f"/api/people/{person}").json()
    assert page["items"] == 1
    assert {a["verb"]: a["count"] for a in page["your_activity"]} == {"watched": 3, "liked": 1}


class TestBylines:
    """Items say who they are by; you by your short name (#268 themes PR)."""

    def _own_note(self, conn):
        return conn.execute(
            "INSERT INTO items (source_type, source_id, content, is_own_content)"
            " VALUES ('voice', 'v-1', 'a voice note', 1)"
        ).lastrowid

    def test_your_own_note_is_by_you_with_your_short_name(self, client, test_config):
        with get_connection(test_config) as conn:
            me = _person(conn, "Matthew Thompson", "person", is_self=1)
            note = self._own_note(conn)
            conn.commit()
        item = client.get(f"/api/items/{note}").json()
        assert (item["by_id"], item["by_name"], item["by_self"]) == (me, "Matthew", True)
        page = client.patch(f"/api/people/{me}", json={"short_name": " Matt "}).json()
        assert page["short_name"] == "Matt"
        assert client.get(f"/api/items/{note}").json()["by_name"] == "Matt"
        # Empty goes back to the first word of the name
        assert (
            client.patch(f"/api/people/{me}", json={"short_name": ""}).json()["short_name"]
            == "Matthew"
        )
        assert client.patch("/api/people/999999", json={"short_name": "x"}).status_code == 404

    def test_a_linked_author_is_named_in_full(self, client, test_config):
        with get_connection(test_config) as conn:
            _person(conn, "Matthew Thompson", "person", is_self=1)
            ada = _person(conn, "Ada Lovelace", "person")
            note = self._own_note(conn)
            conn.execute(
                "INSERT INTO item_agents (item_id, person_id, role, origin)"
                " VALUES (?, ?, 'author', 'test')",
                (note, ada),
            )
            conn.commit()
        item = client.get(f"/api/items/{note}").json()
        assert (item["by_id"], item["by_name"], item["by_self"]) == (ada, "Ada Lovelace", False)

    def test_a_video_has_no_byline(self, client, test_config):
        video = _seed_day(test_config)
        with get_connection(test_config) as conn:
            _person(conn, "Matthew Thompson", "person", is_self=1)
            conn.commit()
        assert client.get(f"/api/items/{video}").json()["by_id"] is None


def test_watches_count_items_and_times(client, test_config):
    """A channel page says how many of their videos you watched, and how often."""
    video = _seed_day(test_config)  # three watches of one video, 9 and 10 Mar
    with get_connection(test_config) as conn:
        channel = _person(conn, "TechChannel", "organization")
        conn.execute(
            "INSERT INTO item_agents (item_id, person_id, role, origin)"
            " VALUES (?, ?, 'creator', 'test')",
            (video, channel),
        )
        conn.commit()
    watched = next(
        a
        for a in client.get(f"/api/people/{channel}").json()["your_activity"]
        if a["verb"] == "watched"
    )
    assert (watched["count"], watched["items"]) == (3, 1)
    assert watched["first_at"] == "2026-03-09T08:00:00.000Z"


def test_a_prompt_shows_what_you_asked(client, test_config):
    """A prompted entry carries the start of your turn at that time."""
    with get_connection(test_config) as conn:
        chat = conn.execute(
            "INSERT INTO items (source_type, source_id, content) VALUES ('gemini', 'c', 'chat')"
        ).lastrowid
        for role, text in (("user", "How far is France?"), ("model", "Quite far.")):
            conn.execute(
                "INSERT INTO items (source_type, source_id, content, parent_id, created_at,"
                " metadata) VALUES ('gemini', ?, ?, ?, '2026-03-10 09:00:00.500000+00:00', ?)",
                (f"t-{role}", text, chat, f'{{"role": "{role}"}}'),
            )
        conn.execute(
            "INSERT INTO activities (verb, item_id, occurred_at, source)"
            " VALUES ('prompted', ?, '2026-03-10T09:00:00.500Z', 'test')",
            (chat,),
        )
        conn.commit()
    day = client.get(
        "/api/timeline", params={"start": "2026-03-10T00:00:00Z", "end": "2026-03-11T00:00:00Z"}
    ).json()
    prompted = [e for e in day["entries"] if e["verb"] == "prompted"]
    assert [e["prompt"] for e in prompted] == ["How far is France?"]
